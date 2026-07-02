import time
import hashlib
import orjson
import sys
import os

# Add the current directory to sys.path to import comet
sys.path.append(os.getcwd())

from comet.utils.cache import generate_etag


def verify():
    print("--- ETag Verification ---")
    data = {"test": "data", "numbers": [1, 2, 3]}
    etag = generate_etag(data)
    print(f"Generated ETag: {etag}")

    # Check format: W/"<16 hex chars>"
    assert etag.startswith('W/"')
    assert etag.endswith('"')
    hash_part = etag[3:-1]
    print(f"Hash part: {hash_part}")
    assert len(hash_part) == 16
    int(hash_part, 16)  # Should not raise ValueError
    print("Format verification: PASSED")

    print("\n--- Performance Comparison ---")
    # Typical stream response size: ~50KB
    large_data = {
        "streams": [
            {
                "name": f"Stream {i}",
                "description": "High quality stream with lots of metadata",
                "url": f"https://example.com/playback/{i}",
            }
            for i in range(100)
        ]
    }
    content = orjson.dumps(large_data)
    print(f"Content size: {len(content) / 1024:.2f} KB")

    iters = 10000

    # Benchmark MD5 (using hashlib directly for comparison)
    start = time.perf_counter()
    for _ in range(iters):
        hashlib.md5(content, usedforsecurity=False).hexdigest()[:16]
    md5_time = (time.perf_counter() - start) * 1000
    print(
        f"hashlib.md5: {md5_time:.2f} ms total, {md5_time * 1000 / iters:.2f} us per hash"
    )

    # Benchmark xxhash (using the optimized generate_etag)
    start = time.perf_counter()
    for _ in range(iters):
        generate_etag(large_data)
    xxhash_time = (time.perf_counter() - start) * 1000
    print(
        f"generate_etag (xxhash): {xxhash_time:.2f} ms total, {xxhash_time * 1000 / iters:.2f} us per hash"
    )

    improvement = (md5_time - xxhash_time) / md5_time * 100
    print(f"\nImprovement: {improvement:.2f}%")

    if improvement > 0:
        print("Optimization verification: PASSED")
    else:
        print("Optimization verification: FAILED (xxhash was not faster in this run)")


if __name__ == "__main__":
    verify()
