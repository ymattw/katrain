# Automatic KataGo thread tuning

Design document for `engine/auto_threads`, the option that derives KataGo's
analysis thread counts from the number of CPU cores.

## 1. Summary

- On local engine startup, KaTrain computes `numAnalysisThreads` and
  `numSearchThreads` from `os.cpu_count()` and passes them to KataGo with
  `-override-config`.
- The total number of search threads is `numAnalysisThreads * numSearchThreads`.
  The calculation keeps that product at roughly one less than the logical CPU
  count, leaving headroom for the GUI, WindowServer and other applications.
- `engine/auto_threads` defaults to `true`. The value is read with
  `config.get("auto_threads", True)`, so configs created before this option
  existed also get automatic tuning without any migration.
- The Engine settings dialog has an **Auto-tune thread counts** checkbox.
- Setting `engine/auto_threads` to `false` disables all thread overrides, so
  KataGo uses whatever `analysis_config.cfg` specifies (12 positions and 8
  threads per position in the bundled config).

## 2. Problem

The bundled `katrain/KataGo/analysis_config.cfg` contains:

```ini
numAnalysisThreads = 12
numSearchThreads = 8
```

That is 96 search threads and allows GPU batches of up to 96 positions. On a
10-core Apple Silicon machine, loading an SGF starts full-game analysis for
every node. The resulting burst makes macOS unresponsive for a while. By
comparison, LizzieYzy Next ships an analysis config with 2 parallel positions
and 16 search threads per position (32 total).

Thread counts are startup-only settings for the KataGo analysis engine. They
cannot be changed per query, and changing them at runtime would require
restarting the engine, reloading the model and discarding queued analysis.
Choosing a sensible count at startup is therefore the practical approach.

## 3. Algorithm

`auto_thread_settings()` in `katrain/core/engine.py`:

```python
def auto_thread_settings(cpu_count=None):
    cores = max(2, int(cpu_count or os.cpu_count() or 4))
    budget = max(2, cores - 1)
    positions = 2 if budget >= 8 else 1
    threads = max(1, budget // positions)
    while positions * threads > budget:
        threads -= 1
    return positions, threads
```

The result is `(numAnalysisThreads, numSearchThreads)`:

| logical cores | positions | threads per position | total search threads |
| ------------- | --------- | -------------------- | -------------------- |
| 4             | 1         | 3                    | 3                    |
| 8             | 1         | 7                    | 7                    |
| 10            | 2         | 4                    | 8                    |
| 16            | 2         | 7                    | 14                   |
| 32            | 2         | 15                   | 30                   |

Two parallel positions are used when the budget allows it, because searching
different positions in parallel is more efficient than running many threads on
the same MCTS tree. On smaller machines the whole budget goes to one position,
which gives more threads (and therefore lower latency) for the position that is
currently being reviewed.

The chosen values are logged at debug level when the engine starts:

```text
Auto thread settings for 10 CPUs: 2 positions x 4 threads
```

## 4. Configuration and UI

- `engine/auto_threads` defaults to `true`.
- The checkbox in the Engine settings section binds to this key.
- `LabelledCheckBox.default_value` was added so that a config which does not
  contain the key shows the checkbox as checked. Without it, opening the
  settings dialog would display an unchecked box and saving would silently write
  `false`.
- Changing the checkbox is one of the settings that restarts the engine, which
  is required because the thread counts are command line overrides.

## 5. Why CPU usage is not used as feedback

Targeting a CPU percentage, for example "grow the thread count until the process
uses 85% CPU", is not a good control signal here:

- The Metal backend is GPU-bound. Search threads spend most of their time
  waiting for neural net evaluations, and one neural net server thread accounts
  for most of the CPU time. Increasing the thread count does not scale CPU usage
  linearly.
- On a 10-core machine, 85% of the whole system means 8.5 busy cores, which this
  workload does not produce. Activity Monitor also reports a single process
  relative to one core, where 100% means one fully used core.
- Reacting to CPU usage would mean restarting KataGo repeatedly. Each restart
  reloads the model and cancels outstanding analysis, so full-game analysis
  would make little progress.

The static core-based budget is predictable and avoids those problems. If
throughput needs measuring, visits per second or GPU utilization are more
meaningful than CPU percentage.

## 6. Limitations and possible extensions

- `os.cpu_count()` reports logical CPUs. On hybrid CPUs (performance plus
  efficiency cores) and on SMT systems it does not distinguish core types.
- The current formula is intentionally conservative. It prioritizes keeping the
  desktop usable over maximizing analysis throughput.
- A future version could run a short startup benchmark and pick the thread
  counts that maximize visits per second, but that would add startup latency and
  another moving part.
- `engine/auto_threads = false` remains the escape hatch for users who want the
  values from `analysis_config.cfg`.
