import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient


spec = importlib.util.spec_from_file_location(
    "junk_cleaner", Path(__file__).resolve().parents[1] / "junk-cleaner" / "main.py"
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class Context:
    def __init__(self, settings_path):
        self.router = APIRouter()
        self.settings_path = settings_path

    def get_settings(self):
        return json.loads(self.settings_path.read_text())

    def save_settings(self, settings):
        self.settings_path.write_text(json.dumps(settings))
        return settings

    def get_config(self, *_):
        raise AssertionError("The cleanup plugin must not read application settings")

    def log_error(self, *_):
        pass


class JunkCleanerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.scan = self.root / "downloads"
        self.scan.mkdir()
        self.settings = self.root / "config.json"
        self.settings.write_text(json.dumps({"scan_paths": [str(self.scan)]}))
        self.ctx = Context(self.settings)
        self.plugin = module.Plugin()
        self.plugin.setup(self.ctx)
        app = FastAPI()
        app.include_router(self.ctx.router)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def file(self, name, root=None):
        path = (root or self.scan) / name
        path.write_text("fixture")
        return path

    def save(self, **settings):
        response = self.client.post("/settings", json=settings)
        self.assertEqual(response.status_code, 200)
        return response.json()

    def test_upgrade_keeps_paths_and_requires_explicit_plugin_keywords(self):
        self.file("广告.txt")
        settings = self.client.get("/settings").json()
        self.assertEqual(settings, {"scan_paths": str(self.scan), "keywords": ""})
        result = self.client.post("/scan", json={}).json()
        self.assertEqual(result["items"], [])
        self.assertIn("本插件", result["message"])

    def test_settings_normalize_and_persist_without_changing_paths(self):
        saved = self.save(keywords="广告， AD,ad\n\n 游戏大全 ")
        self.assertEqual(saved["keywords"], "广告\nAD\n游戏大全")
        self.assertEqual(saved["scan_paths"], str(self.scan))
        reopened = module.Plugin()
        reopened.setup(Context(self.settings))
        self.assertEqual(reopened._keywords(), ["广告", "ad", "游戏大全"])
        self.save(scan_paths=str(self.scan))
        self.assertEqual(self.ctx.get_settings()["keywords"], ["广告", "AD", "游戏大全"])

    def test_scan_uses_only_plugin_keywords_and_matches_filename(self):
        self.save(keywords="广告, AD")
        for name in ("广告.txt", "AD.url", "movie.mkv", "游戏大全.txt"):
            self.file(name)
        result = self.client.post("/scan", json={}).json()
        self.assertEqual({item["name"] for item in result["items"]}, {"广告.txt", "AD.url"})
        self.assertTrue((self.scan / "广告.txt").exists(), "Scanning must not delete")

    def test_delete_rechecks_current_keywords_and_stays_inside_roots(self):
        self.save(keywords="广告")
        junk = self.file("广告.txt")
        keep = self.file("movie.mkv")
        outside = self.file("广告-outside.txt", self.root)
        result = self.client.post("/delete", json={"paths": [str(junk), str(keep), str(outside)]})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()["removed"], [str(junk)])
        self.assertTrue(keep.exists())
        self.assertTrue(outside.exists())

    def test_changed_or_cleared_keywords_prevent_deleting_old_scan_results(self):
        self.save(keywords="广告")
        junk = self.file("广告.txt")
        self.save(keywords="游戏大全")
        result = self.client.post("/delete", json={"paths": [str(junk)]})
        self.assertEqual(result.json()["removed"], [])
        self.save(keywords="")
        result = self.client.post("/delete", json={"paths": [str(junk)]})
        self.assertEqual(result.status_code, 422)
        self.assertTrue(junk.exists())

    def test_temporary_scan_path_uses_same_independent_keywords(self):
        self.save(keywords=["广告"])
        other = self.root / "other"
        other.mkdir()
        junk = self.file("广告.txt", other)
        self.file("movie.mkv", other)
        result = self.client.post("/scan", json={"path": str(other)}).json()
        self.assertEqual([item["path"] for item in result["items"]], [str(junk)])
        deleted = self.client.post("/delete", json={"paths": [str(junk)], "path": str(other)})
        self.assertEqual(deleted.json()["removed"], [str(junk)])


if __name__ == "__main__":
    unittest.main()
