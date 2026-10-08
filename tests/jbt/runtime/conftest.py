import pytest

from jbt.domain.canonical import encode_json
from jbt.runtime.execution import ObservedExecution, decode_execution


@pytest.fixture
def release_execution() -> ObservedExecution:
    return decode_execution(
        encode_json(
            {
                "schema_version": 1,
                "producer": {
                    "kind": "release",
                    "name": "jbt",
                    "version": "0.1.0a1",
                    "source_commit": "a" * 40,
                },
                "runtime": {
                    "implementation": "cpython",
                    "version": "3.14.7",
                    "platform": "linux",
                    "architecture": "x86_64",
                },
                "distributions": [{"name": "jbt", "version": "0.1.0a1"}],
            },
            integer_strings=False,
        )
    )
