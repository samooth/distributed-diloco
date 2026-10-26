from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from diloco.object_store import LocalObjectStore, sync_directory


class ObjectStoreTests(unittest.TestCase):
    def test_put_get_list_delete(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = LocalObjectStore(tmp)
            store.put("a/b.txt", b"hello")
            self.assertEqual(store.get("a/b.txt"), b"hello")
            self.assertEqual(store.list("a"), ["a/b.txt"])
            store.delete("a/b.txt")
            with self.assertRaises(FileNotFoundError):
                store.get("a/b.txt")

    def test_sync_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source"
            (source / "nested").mkdir(parents=True)
            (source / "one.txt").write_bytes(b"1")
            (source / "nested" / "two.txt").write_bytes(b"2")
            store = LocalObjectStore(Path(tmp) / "store")
            uploaded = sync_directory(store, source)
            self.assertEqual(uploaded, ["nested/two.txt", "one.txt"])
            self.assertEqual(store.get("one.txt"), b"1")
            self.assertEqual(store.get("nested/two.txt"), b"2")


if __name__ == "__main__":
    unittest.main()
