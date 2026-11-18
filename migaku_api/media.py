"""Bounded, content-addressed pen JPEG staging; no remote URLs or paths."""
import base64
import hashlib
import io
import os
import re
import tempfile
import json
import time
from pathlib import Path

from PIL import Image

from .browser import WorkerError


class MediaStore:
    def __init__(self, directory):
        self.directory = Path(directory)

    @staticmethod
    def filename(reference):
        digest = reference.get("sha256", "")
        if not re.fullmatch(r"[a-f0-9]{64}", digest) or reference.get("filename") != f"pen_{digest}.jpg":
            raise WorkerError("anki_photo_invalid", "Invalid pen photo reference.")
        return reference["filename"]

    def save(self, photo):
        try:
            data = base64.b64decode(photo.data, validate=True)
            if not 0 < len(data) <= 400000 or hashlib.sha256(data).hexdigest() != photo.sha256:
                raise ValueError("size or digest")
            with Image.open(io.BytesIO(data)) as image:
                if image.format != "JPEG" or image.width * image.height > 1000000:
                    raise ValueError("format or dimensions")
                image.verify()
            # Fully decode too: verify() alone does not detect every truncated JPEG.
            with Image.open(io.BytesIO(data)) as image:
                image.load()
        except Exception as exc:
            raise WorkerError("anki_photo_invalid", "Pen photo is not a valid bounded JPEG.") from exc
        reference = {"filename": f"pen_{photo.sha256}.jpg", "sha256": photo.sha256, "bytes": len(data)}
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = self.directory / reference["filename"]
        fd, temporary = tempfile.mkstemp(prefix=".photo-", dir=self.directory)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return reference

    def read(self, reference):
        path = self.directory / self.filename(reference)
        try:
            if path.is_symlink() or not 0 < path.stat().st_size <= 400000:
                raise ValueError("size")
            data = path.read_bytes()
            if len(data) != reference["bytes"] or hashlib.sha256(data).hexdigest() != reference["sha256"]:
                raise ValueError("digest")
            return data
        except Exception as exc:
            raise WorkerError("anki_photo_missing", "Saved pen photo needs attention; no card was discarded.") from exc

    def prune(self, store):
        # Never remove an image referenced by a pending/uncertain job. This is a
        # staging cache only; Anki owns its independently verified media copy.
        if not self.directory.exists():
            return
        active = set()
        for row in store.db.execute("SELECT payload,prepared_payload FROM jobs WHERE state!='synced'"):
            for data, key in ((row[0], 'photo'), (row[1], 'pen_photo')):
                reference = json.loads(data).get(key) if data else None
                if reference:
                    active.add(self.filename(reference))
        cutoff = time.time() - 7 * 86400
        for path in self.directory.glob("pen_*.jpg"):
            if path.name not in active and not path.is_symlink() and path.stat().st_mtime < cutoff:
                path.unlink()
