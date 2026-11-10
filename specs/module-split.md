# Spec: module-split

## Goal
Split the six largest source files into smaller, focused modules and break their longest functions into private helpers, so each concern can be read and changed on its own. This is a pure refactor for the developer maintaining the bot: no behavior, logic, API, log, database or UI change.

## User stories / requirements
- As the developer maintaining the bot, I want the largest modules split by concern so I can find and change one piece without scrolling through 500 to 1,000 lines.
- R1: Each target file ends at or under its line cap (`wc -l`):

  | File | Lines now | Cap |
  |------|-----------|-----|
  | `backend/app/core/execution/order_manager.py` | 1058 | 450 |
  | `frontend/src/pages/Backtesting.tsx` | 845 | 400 |
  | `backend/app/core/strategy_engine/scheduler.py` | 592 | 400 |
  | `backend/app/brokers/ibkr/client.py` | 577 | 400 |
  | `backend/app/api/v1/backtesting.py` | 549 | 350 |
  | `backend/app/core/backtesting/engine.py` | 472 | 350 |

- R2: Every file created by this change is at most 450 lines.
- R3: No Python function or method in the five backend target files, or in any backend module this change creates, spans more than 80 lines (`node.end_lineno - node.lineno + 1` from `ast`). Over the cap today: `OrderManager.submit_order` 196, `OrderManager.close_position` 156, `OrderManager.finalize_close` 110, `StrategyScheduler._run_strategy_for_symbol` 151, `StrategyScheduler._run_cycle` 93, `BacktestingEngine.run` 163, `run_portfolio_backtest` 96.
- R4: No function or React component in `frontend/src/pages/Backtesting.tsx` or in a `.ts`/`.tsx` file this change creates spans more than 150 lines. Measured with the TypeScript compiler API over every `FunctionDeclaration`, `FunctionExpression`, `ArrowFunction` and `MethodDeclaration` node (nested ones included, each measured on its own), as end line minus start line plus 1, where the start is `node.getStart()` (leading comments excluded) and the end is `node.getEnd()`. `Backtesting()` measures 371 lines today.
- R5: Extraction only. Code moves into new modules or new private helpers verbatim: same statements, same order, same conditions, same log messages and `extra={...}` keys, same exception types and messages, same `await` points, same `Decimal` handling. Allowed edits are limited to: import lines; the `def`/`class` line of a new helper and the call that replaces the moved block; passing in the locals the block reads and returning the values (or an early-exit signal) the caller needs; mixin base classes on an existing class; re-export lines; and one short docstring line per new public function or class. No renames of existing names, no reformatting of code that is not moved, no "while I'm here" fixes.
- R6: Every name importable today from the six target modules stays importable from the same path, re-exported from the original module when its definition moves. This includes (at least): from `app.core.execution.order_manager`: `OrderManager`, `CloseOutcome`, `TRADE_EVENTS_CHANNEL`; from `app.core.strategy_engine.scheduler`: `StrategyScheduler`, `_CONSECUTIVE_MISS_ESCALATION_THRESHOLD`; from `app.brokers.ibkr.client`: `IBKRClient`, `LiveTradingGuardError`; from `app.api.v1.backtesting`: `router`, `registry`, `OHLCVRepo`, `BacktestingEngine`, `BacktestRunRequest`, `_Job`, `_build_costs`, `_run_job`, `_run_job_locked`, `_serialise_metrics`; from `app.core.backtesting.engine`: `BacktestingEngine`, `BacktestMetrics`, `BacktestResult`, `SimulatedTrade`, `_compute_metrics`, `apply_slippage`, `exit_fill`, `leg_commission`, `visible_extra_bars`.
- R7: Test patch points keep working. Tests replace these module globals by dotted path, and a re-export does not carry a patch, so the code that reads each one must stay in the module named here and look the name up there at call time:
  - `app.core.execution.order_manager.audit_logger`: `_write_pre_submission`, `_write_post_confirmation`, and every audit write reached from `submit_order` and `close_position`.
  - `app.core.execution.order_manager.error_logger`: the audit-write-failure `error_logger.critical` reached from `submit_order`.
  - `app.core.strategy_engine.scheduler.system_logger`: the missing-price warning and its escalation to error in the per-symbol run (today inside `_run_strategy_for_symbol`).
  - `app.core.strategy_engine.scheduler.publish_system_alert`: the `GROWTH_MODE_CHANGED` alert in `_run_cycle`.
  - `app.brokers.ibkr.client._CANCEL_WAIT_SECONDS`: read by `cancel_order`.
- R8: Classes keep every method and attribute that tests touch on instances, and every call that goes through `self.method_name` today still goes through `self.method_name`, so a test that replaces an instance method keeps taking effect. Known names: `StrategyScheduler` `_run_cycle`, `_run_strategy_for_symbol`, `_load_enabled_strategies`, `_get_current_price`, `_fetch_db_bars`, `_get_intraday_bars`, `_get_historical_bars`, `_consecutive_price_misses`, `_broker`, `_cache`, `_signals`, `_order_manager`, `_registry`, `_clock`, `_running`; `IBKRClient` `_on_pending_tickers`, `_supervisor_task`, `_last_emitted`, `_tasks`, `_price_callbacks`, `_ib`, `_subscribed_contracts`, `_settings`; `OrderManager` `submit_order`, `close_position`, `_load_active_trades`, `_is_market_open`. Splitting a class through mixin base classes in new modules, or through module-level helpers called from thin methods, are both acceptable.
- R9: New backend modules live in the same package as the file they came from, or in a new subpackage of it. Module import order must not create a cycle; do not add function-level imports to work around one.
- R10: Frontend: `Backtesting` stays a named export of `frontend/src/pages/Backtesting.tsx` and `frontend/src/App.tsx` does not change. The subcomponents (`SingleJobStatus`, `PortfolioJobStatus`, `PortfolioResults`, `EquityCurve`, `BacktestResults`, `NoHistoricalDataPanel`, `Field`, `MetricCard`) and the local interfaces move into `frontend/src/components/backtesting/`. Rendered markup, Tailwind classes, text, query keys, API calls and request bodies stay identical. State stays owned by `Backtesting()`: it may move into a custom hook that `Backtesting()` calls (hooks called in the same order with the same dependency arrays), and JSX sections may become child components that receive state and setters as props, but no `useState`/`useEffect`/`useQuery` moves into a child component.

## Acceptance criteria
- [ ] AC1: `cd backend && ruff check .` exits 0, and `cd backend && python3 -m pytest tests/unit -q` exits 0 with no failures or errors.
- [ ] AC2: `git diff --name-only master -- backend/tests` prints nothing: no test file is added, changed or deleted.
- [ ] AC3: `cd frontend && npm run typecheck && npm run lint && npm run build` exits 0.
- [ ] AC4: `wc -l` on the six target files shows every one at or under its R1 cap, and `git diff --name-only --diff-filter=A master -- backend/app frontend/src | xargs wc -l` shows no new file over 450 lines.
- [ ] AC5: Both commands print nothing. Backend (R3, no function over 80 lines), run from the repo root:
  `python3 -c "import ast,subprocess,sys; fs={'backend/app/core/execution/order_manager.py','backend/app/core/strategy_engine/scheduler.py','backend/app/brokers/ibkr/client.py','backend/app/api/v1/backtesting.py','backend/app/core/backtesting/engine.py'}|set(subprocess.check_output(['git','diff','--name-only','--diff-filter=A','master','--','backend/app/*.py'],text=True).split()); [print(f,n.name,n.end_lineno-n.lineno+1) for f in sorted(fs) for n in ast.walk(ast.parse(open(f).read())) if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef)) and n.end_lineno-n.lineno+1>80]"`
  Frontend (R4, no function over 150 lines), run from `frontend/`:
  `node -e "const ts=require('typescript'),fs=require('fs'),cp=require('child_process');const fs0=new Set(['src/pages/Backtesting.tsx',...cp.execSync('git diff --name-only --diff-filter=A master -- src',{encoding:'utf8'}).split('\n').filter(f=>/\.tsx?$/.test(f)).map(f=>f.replace(/^frontend\//,''))]);for(const f of [...fs0].sort()){const s=ts.createSourceFile(f,fs.readFileSync(f,'utf8'),ts.ScriptTarget.Latest,true);const v=n=>{if(ts.isFunctionDeclaration(n)||ts.isFunctionExpression(n)||ts.isArrowFunction(n)||ts.isMethodDeclaration(n)){const a=s.getLineAndCharacterOfPosition(n.getStart(s)).line,b=s.getLineAndCharacterOfPosition(n.getEnd()).line,l=b-a+1;if(l>150)console.log(f,(n.name&&n.name.getText(s))||'(anonymous)',l)}ts.forEachChild(n,v)};v(s)}"`
- [ ] AC6: From `backend/`, this exits 0 and prints `ok`:
  `python3 -c "from app.core.execution.order_manager import OrderManager, CloseOutcome, TRADE_EVENTS_CHANNEL; from app.core.strategy_engine.scheduler import StrategyScheduler, _CONSECUTIVE_MISS_ESCALATION_THRESHOLD; from app.brokers.ibkr.client import IBKRClient, LiveTradingGuardError; from app.api.v1.backtesting import router, registry, OHLCVRepo, BacktestingEngine, BacktestRunRequest, _Job, _build_costs, _run_job, _run_job_locked, _serialise_metrics; from app.core.backtesting.engine import BacktestingEngine, BacktestMetrics, BacktestResult, SimulatedTrade, _compute_metrics, apply_slippage, exit_fill, leg_commission, visible_extra_bars; print('ok')"`
- [ ] AC7: Integration tests pass against a `*_test` database, set up exactly as in the README's Development section (`docker compose up -d db redis`, `DATABASE_URL=...trading_bot_test`, `alembic upgrade head && python -m pytest tests/integration -q`), because execution and broker code is touched.
- [ ] AC8: One combined diff over the targets and every extraction destination, `git diff -M --color-moved=zebra --color-moved-ws=allow-indentation-change master -- backend/app frontend/src`, shows every removed line of logic reappearing as a moved line (a change of indentation alone is allowed). Where Git does not mark a block as moved, comparing the old block with its new location side by side shows the same statements in the same order. The only other changes are those R5 allows (imports, new `def`/`class` lines, the replacing calls, parameter/return plumbing, mixin bases, re-exports, one-line docstrings).

## Constraints
- No functional or logic change of any kind (R5). When a block cannot be extracted without changing logic, leave it in place and note it in the task, rather than rewriting it.
- No new dependencies. No edits to files on the AGENTS.md "Do not touch" list, to `backend/tests/`, or to docs (no doc names any target file today).
- All `.claude/CLAUDE.md` guardrails still hold; in particular money stays `Decimal`, no new un-referenced `asyncio.create_task`, and no reserved `LogRecord` keys in `extra={...}` (moved log calls keep their exact keys).
- Ruff rules from `backend/ruff.toml` (line length 100, import sorting with first-party `app`) apply to every new file.
- Run `graphify update .` after code changes so the knowledge graph stays current.
- One target file per task where practical, so each task's diff can be checked on its own against AC8.

## Out of scope
- Every other large file, including `core/risk/manager.py`, `core/engine.py`, `db/repositories/trade_repo.py`, `core/execution/position_monitor.py`, `notifications/dispatcher.py`, `brokers/mock/client.py`, `core/backtesting/portfolio_engine.py`, `pages/Strategies.tsx`, `pages/Watchlist.tsx`.
- Bug fixes, renames, type-annotation improvements, performance work, or new tests, even where the refactor reveals something worth changing (list such findings in the final report instead).
- Changing any test, doc, migration, config, API route, response shape or UI.

## Open questions
- Integration tests (AC7) need Docker running with the `db` and `redis` services; Docker is not running on this machine right now, so start it before the review stage.
- Mixins versus module-level helper functions for splitting `OrderManager`, `StrategyScheduler` and `IBKRClient` is left to the planner, within R7 and R8.
