"""Tests for automatic KataGo thread tuning."""

from katrain.core.auto_threads import auto_thread_settings, resolve_thread_overrides


def test_auto_thread_settings_respects_cpu_budget():
    assert auto_thread_settings(2) == (1, 2)
    assert auto_thread_settings(4) == (1, 3)
    assert auto_thread_settings(8) == (1, 7)
    assert auto_thread_settings(9) == (2, 4)
    assert auto_thread_settings(10) == (2, 4)
    assert auto_thread_settings(12) == (2, 5)
    assert auto_thread_settings(16) == (2, 7)


def test_resolve_thread_overrides_modes():
    # Default: auto_threads enabled
    threads, args = resolve_thread_overrides({}, cpu_count=10)
    assert threads == 2
    assert args == ["-override-config", "numAnalysisThreads=2", "-override-config", "numSearchThreads=4"]

    # Explicit num_analysis_threads overrides auto_threads
    threads, args = resolve_thread_overrides({"num_analysis_threads": 6, "auto_threads": True}, cpu_count=10)
    assert threads == 6
    assert args == ["-override-config", "numAnalysisThreads=6"]

    # Disabled auto_threads leaves analysis_config.cfg untouched
    threads, args = resolve_thread_overrides({"auto_threads": False}, cpu_count=10)
    assert threads == 12
    assert args == []
