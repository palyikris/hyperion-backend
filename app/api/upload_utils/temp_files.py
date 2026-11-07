import os
from collections.abc import Iterable


def cleanup_temp_files(paths: Iterable[str]) -> None:
    for path in set(paths):
        try:
            if path and os.path.exists(path):
                os.remove(path)
        except OSError as cleanup_error:
            print(f"Failed to cleanup temp file {path}: {cleanup_error}")
