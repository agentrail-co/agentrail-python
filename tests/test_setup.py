# Copyright 2026 Agentrail. Licensed under the Apache License, Version 2.0.
"""Startup: options validation, and one installation per process."""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from agentrail_python import ConfigurationError, QueueOptions
from support import options


@pytest.mark.parametrize(
    "change",
    [
        {"collector_endpoint": "ftp://collector.test"},
        {"collector_endpoint": "https://user:pw@collector.test"},
        {"collector_endpoint": "https://collector.test/otlp?x=1"},
        {"hash_key": ""},
        {"service_name": "bad\x00"},
        {"outbound_origins": ("https://inventory.internal/",)},
        {"outbound_origins": ("https://Inventory.internal",)},
        {"outbound_origins": ("https://inventory.internal:443",)},
        {"queue": QueueOptions(batch_size=10, max_records=5)},
        {"queue": QueueOptions(export_timeout=20)},
    ],
)
# Fails if someone accepts a configuration that would leak credentials, sign
# for an inexact origin, or deadlock delivery.
def test_invalid_options(change: dict[str, object]) -> None:
    from agentrail_python import CollectorOptions, init

    endpoint = change.pop("collector_endpoint", "https://collector.test/ingest/otlp")
    with pytest.raises(ConfigurationError):
        init(options("x", collector=CollectorOptions(endpoint=str(endpoint), secret="s"), **change))


INIT = textwrap.dedent(
    """
    import sys
    sys.path.insert(0, {tests!r})
    from support import collector, options
    import agentrail_python as ar
    with collector() as state:
        settings = options(state.url)
        client = ar.init(settings)
        assert ar.init(settings) is client
        try:
            ar.init(options(state.url))
        except ar.ConfigurationError:
            pass
        else:
            raise SystemExit("second init accepted")
        client.milestone("standalone", ar.MilestoneContext(surface="custom", kind="k"))
        client.shutdown()
        assert [span["name"] for span in state.spans()] == ["standalone"], state.spans()
    print("ok")
    """
)


# Fails if someone lets init accept a second configuration, which would record
# every request twice, or stops returning the installed client.
def test_init_in_a_fresh_process() -> None:
    tests = str(Path(__file__).parent)
    result = subprocess.run(  # noqa: PLW1510 - the exit code is asserted
        [sys.executable, "-c", INIT.format(tests=tests)],
        capture_output=True,
        text=True,
        timeout=60,
        env=os.environ.copy(),
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"
