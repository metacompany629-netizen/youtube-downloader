"""Storage for prepared files: Cloudflare R2 (S3-compatible) when the
R2_* env vars are set, otherwise local disk.

R2 env vars: R2_ENDPOINT, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET.
Local fallback dir: JOBS_DIR (default ./data/jobs).

Placeholder values (e.g. "placeholder", "https://placeholder") count as
UNSET, so a host that forces all detected env vars to be filled can be
given dummy R2 values without switching the app off local disk.
"""
import os

_PLACEHOLDERS = {"", "placeholder", "disabled", "none", "unset",
                 "dummy", "-", "n/a", "null"}


def _is_placeholder(value):
    if not value:
        return True
    v = value.strip().lower()
    return v in _PLACEHOLDERS or "placeholder" in v

LOCAL_DIR = os.path.abspath(os.environ.get(
    "JOBS_DIR",
    os.path.join(os.path.dirname(__file__), "..", "data", "jobs")))


def r2_enabled():
    """True only when all four R2 vars hold real (non-placeholder) values."""
    return all(
        not _is_placeholder(os.environ.get(key))
        for key in ("R2_ENDPOINT", "R2_ACCESS_KEY_ID",
                    "R2_SECRET_ACCESS_KEY", "R2_BUCKET"))


def _r2_client():
    import boto3
    return boto3.client(
        "s3",
        endpoint_url=os.environ["R2_ENDPOINT"],
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
    )


class Store:
    """One prepared file: either a local path or an R2 object key."""

    def __init__(self):
        self.use_r2 = r2_enabled()
        self.bucket = os.environ.get("R2_BUCKET") if self.use_r2 else None
        self.local_dir = LOCAL_DIR
        os.makedirs(self.local_dir, exist_ok=True)

    def local_path(self, job_id, filename):
        d = os.path.join(self.local_dir, job_id)
        os.makedirs(d, exist_ok=True)
        return os.path.join(d, filename)

    def put(self, job_id, filename, local_file):
        """Store a prepared local file. Returns (key, size)."""
        size = os.path.getsize(local_file)
        if self.use_r2:
            key = "jobs/%s/%s" % (job_id, filename)
            _r2_client().upload_file(local_file, self.bucket, key)
            try:
                os.remove(local_file)
            except OSError:
                pass
            return key, size
        return local_file, size

    def open_stream(self, key, start=None, end=None):
        """Return (iterator_of_bytes, size). start/end are inclusive."""
        if self.use_r2:
            kwargs = {"Bucket": self.bucket, "Key": key}
            if start is not None:
                rng = "bytes=%d-" % start
                if end is not None:
                    rng = "bytes=%d-%d" % (start, end)
                kwargs["Range"] = rng
            obj = _r2_client().get_object(**kwargs)
            total = int(obj.get("ContentLength") or 0)
            return obj["Body"].iter_chunks(chunk_size=1024 * 256), total
        size = os.path.getsize(key)
        f = open(key, "rb")
        if start is not None:
            f.seek(start)
        remaining = size if start is None else size - start
        if end is not None:
            remaining = min(remaining, end - (start or 0) + 1)

        def gen():
            try:
                left = remaining
                while left > 0:
                    chunk = f.read(min(1024 * 256, left))
                    if not chunk:
                        break
                    left -= len(chunk)
                    yield chunk
            finally:
                f.close()

        return gen(), remaining

    def file_size(self, key):
        if self.use_r2:
            head = _r2_client().head_object(Bucket=self.bucket, Key=key)
            return int(head.get("ContentLength") or 0)
        return os.path.getsize(key)

    def delete(self, key):
        try:
            if self.use_r2:
                _r2_client().delete_object(Bucket=self.bucket, Key=key)
            elif os.path.exists(key):
                os.remove(key)
                parent = os.path.dirname(key)
                try:
                    os.rmdir(parent)
                except OSError:
                    pass
        except Exception:
            pass
