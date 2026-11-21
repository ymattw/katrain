"""Automatic KataGo analysis/search thread tuning based on CPU count."""

import os


def auto_thread_settings(cpu_count=None):
    """Pick KataGo thread counts that leave roughly one CPU core for the system.

    The total number of KataGo search threads is ``positions * threads``, so this
    returns ``(positions, threads)`` with that product at most
    ``max(2, cpu_count - 1)``. Two parallel positions are used when there is
    enough budget, because searching different positions in parallel is more
    efficient than many threads on one tree.
    """
    cores = max(2, int(cpu_count or os.cpu_count() or 4))
    budget = max(2, cores - 1)
    positions = 2 if budget >= 8 else 1
    threads = max(1, budget // positions)
    while positions * threads > budget:
        threads -= 1
    return positions, threads


def resolve_thread_overrides(config, log=None, cpu_count=None):
    """Return ``(analysis_threads, cli_override_args)`` for a KataGo config dict.

    Limiting threads keeps the GPU batch and CPU contention small enough for the
    desktop to stay responsive. When ``auto_threads`` is disabled and no explicit
    ``num_analysis_threads`` is configured, ``analysis_config.cfg`` is used as-is.
    """
    if config.get("num_analysis_threads"):
        analysis_threads = max(1, int(config["num_analysis_threads"]))
        return analysis_threads, ["-override-config", f"numAnalysisThreads={analysis_threads}"]

    if config.get("auto_threads", True):
        analysis_threads, search_threads = auto_thread_settings(cpu_count=cpu_count)
        if log is not None:
            detected = cpu_count if cpu_count is not None else os.cpu_count()
            log(f"Auto thread settings for {detected} CPUs: {analysis_threads} positions x {search_threads} threads")
        return analysis_threads, [
            "-override-config",
            f"numAnalysisThreads={analysis_threads}",
            "-override-config",
            f"numSearchThreads={search_threads}",
        ]

    return 12, []
