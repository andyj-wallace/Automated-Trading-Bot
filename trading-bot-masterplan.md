# Algorithmic Trading Bot System Masterplan

## 1. Overview and Objectives
The system is a personal algorithmic trading bot designed to execute and manage trading strategies for stocks, with future expansion to options trading. The primary goals are:
- Implement and manage multiple trading strategies with configurable automation levels
- Maintain strict risk management controls
- Provide comprehensive monitoring and analysis tools
- Support strategy backtesting capabilities
- Ensure system modularity for future expansion

## 2. Core Features and Functionality

### 2.1 Trading Strategy Management
- Initial support for three strategies:
  - 50 vs 200 day moving average
  - Stock trend vs 200 day moving average
  - Mean reversion prediction
- Future expansion to include:
  - Iron condor options strategy
  - Bull/bear market prediction
  - Intra-week mean reversion
- Visual interface for strategy parameter configuration
- Strategy enabling/disabling capabilities
- Strategy chaining/combination functionality
- Performance visualization and comparison tools

### 2.2 Risk Management System
- Portfolio-wide risk metrics
- 1% maximum loss per trade rule
- Position size limits
- Maximum concurrent trades limit
- Comprehensive risk logging and reporting
- Real-time risk threshold monitoring

### 2.3 Monitoring and Analytics
- Real-time dashboard displaying:
  - Risk metrics relative to 1% threshold (primary focus)
  - Active trades with current P&L
  - Strategy performance metrics
  - System alerts and notifications
- Historical data analysis:
  - Trading history
  - Strategy performance
  - Risk management logs
  - Market data (temporary storage with overwrite capability)
- System health monitoring:
  - Crash logs
  - Request failures
  - Application errors

### 2.4 Backtesting System
- One-year historical data support
- Market condition simulation capabilities
- Performance comparison tools
- Strategy optimization features

### 2.5 Notification System
- Trade execution alerts
- Risk threshold warnings
- Strategy performance updates
- System health alerts
- Multi-channel delivery (email/mobile)

## 3. Technical Architecture

### 3.0 Recommended Tech Stack

#### Backend
- **Primary Language**: Python
  - Excellent for financial calculations and data analysis
  - Rich ecosystem of financial libraries (pandas, numpy, scipy)
  - Strong async capabilities for handling real-time data
  - Great integration with machine learning libraries for future expansion

- **Framework**: FastAPI
  - High performance async framework
  - Excellent for building REST APIs
  - Built-in WebSocket support for real-time updates
  - Automatic API documentation
  - Type hints for better code reliability

- **Database**:
  - Primary: PostgreSQL
    - Robust relational database for trading history and user data
    - Strong support for time-series data
    - JSONB support for flexible strategy configuration storage
  - TimescaleDB extension
    - Optimized for time-series data (market data, performance metrics)
  - Redis
    - In-memory cache for real-time data
    - Pub/sub capabilities for real-time updates

#### Frontend
- **Framework**: React with TypeScript
  - Strong type safety
  - Rich ecosystem of charting libraries
  - Excellent state management options
  - Progressive Web App (PWA) capabilities for future mobile support

- **UI Components**:
  - TradingView charting library for technical analysis
  - Material-UI or Tailwind CSS for responsive design
  - React Query for efficient data fetching and caching

#### Initial Infrastructure
- **Caching**: Redis
  - In-memory cache for real-time data
  - Pub/sub capabilities for real-time updates
  - Simple setup and maintenance

- **Logging & Monitoring**: Built-in Application Monitoring
  - Basic application logging with Python's logging module
  - Simple metrics dashboard in the web interface
  - Custom alert system
  

## 3.1 System Components

### 3.1 System Components
- Web API Layer
  - Broker API integration (Initially Interactive Brokers)
  - Modular design for future broker API additions
- Strategy Engine
  - Strategy execution module
  - Risk management module
  - Backtesting module
- Data Management System
  - Market data handler
  - Historical data storage
  - Performance metrics database
- Web Interface
  - Real-time dashboard
  - Strategy management UI
  - Analytics visualization
- Notification Service
  - Alert management
  - Multi-channel delivery

### 3.2 Data Model Concepts
- Trading Strategies
  - Strategy parameters
  - Execution rules
  - Performance metrics
- Trades
  - Entry/exit points
  - Position sizing
  - Risk metrics
- Portfolio
  - Active positions
  - Risk exposure
  - Performance history
- System Logs
  - Trading activity
  - Risk management
  - System health

### 3.3 Integration Points
- Broker APIs
  - Interactive Brokers (initial)
  - Extensible for additional brokers
- Market Data Providers
- Notification Services

## 4. Development Roadmap

### Phase 1: Core Infrastructure (4-6 weeks)
1. **Project Setup & Basic Architecture** (Week 1)
   - Set up development environment with Python/FastAPI and React/TypeScript
   - Initialize database with PostgreSQL/TimescaleDB
   - Establish basic project structure and CI/CD pipeline

2. **Data Infrastructure** (Week 2)
   - Implement market data models
   - Set up data fetching from Interactive Brokers API
   - Create basic data storage and retrieval system

3. **Basic Trading Engine** (Weeks 3-4)
   - Implement basic trade execution system
   - Create fundamental risk management checks
   - Develop simple logging system
   - Build basic error handling

4. **Simple Web Interface** (Weeks 5-6)
   - Create dashboard structure
   - Implement basic market data display
   - Add simple trade monitoring view
   - Set up WebSocket for real-time updates

### Phase 2: Strategy Implementation (4-5 weeks)
1. **First Strategy Implementation** (Weeks 1-2)
   - Implement moving average strategy
   - Create strategy configuration system
   - Develop strategy testing framework
   - Add basic performance monitoring

2. **Risk Management System** (Weeks 2-3)
   - Implement 1% rule calculation
   - Add position size limiting
   - Create risk monitoring dashboard
   - Set up risk alerts

3. **Strategy Management Interface** (Weeks 3-5)
   - Build strategy configuration UI
   - Add strategy enable/disable functionality
   - Implement basic strategy performance visualization
   - Create strategy parameter adjustment interface

### Phase 3: Advanced Features (6-8 weeks)
1. **Backtesting System** (Weeks 1-3)
   - Implement historical data management
   - Create backtesting engine
   - Build backtesting results visualization
   - Add strategy optimization tools

2. **Enhanced Monitoring** (Weeks 3-5)
   - Implement comprehensive logging
   - Create detailed performance analytics
   - Add advanced risk metrics
   - Build notification system

3. **System Optimization** (Weeks 5-8)
   - Optimize database queries
   - Improve real-time performance
   - Enhance error handling
   - Add system health monitoring

### Phase 4: Expansion & Refinement (4-6 weeks)
1. **Additional Strategies** (Weeks 1-2)
   - Add mean reversion strategy
   - Implement trend analysis
   - Create strategy combination framework

2. **Advanced Analytics** (Weeks 2-4)
   - Implement advanced performance metrics
   - Add market analysis tools
   - Create detailed reporting system

3. **System Hardening** (Weeks 4-6)
   - Enhance security measures
   - Improve error recovery
   - Add automated testing
   - Optimize system performance

### Phase 5: Future Enhancements (Ongoing)
- Mobile interface development
- Options trading capabilities
- Machine learning integration
- Additional broker integrations
- Advanced strategy features

### Key Dependencies and Considerations
- Each phase builds upon the previous one
- Core infrastructure must be solid before adding complex strategies
- Risk management systems should be thoroughly tested at each stage
- Regular testing and validation throughout development
- Flexibility to adjust timeline based on complexity and testing results

## 5. Technical Considerations

### 5.1 Scalability
- Initial support for 5 stocks and 3 strategies
- Designed for easy expansion
- Efficient data management
- Performance optimization capabilities

### 5.2 Security
- Secure API key management
- Trading verification systems
- Data encryption
- Access controls

### 5.3 Reliability
- Comprehensive error handling
- System health monitoring
- Failure recovery procedures
- Data backup systems

## 6. Future Expansion Possibilities
- Options trading capabilities
- Additional broker integrations
- Mobile application
- Advanced market analysis tools
- Machine learning integration
- High-frequency trading capabilities
- Enhanced backtesting features

## 7. Potential Challenges and Solutions

### 7.1 Data Management
Challenge: Efficient handling of market data and trading history
Solution: Implement efficient data storage with cleanup procedures

### 7.2 Strategy Complexity
Challenge: Managing multiple concurrent strategies
Solution: Modular strategy design with clear interfaces

### 7.3 Risk Management
Challenge: Real-time risk calculation across multiple positions
Solution: Efficient risk calculation algorithms with caching

### 7.4 System Reliability
Challenge: Ensuring consistent operation during market hours
Solution: Robust error handling and recovery procedures

### 7.5 API Integration
Challenge: Managing multiple broker APIs
Solution: Abstract broker interface design


### 8. Hardening
Review complete — sweep found nothing new beyond the verified list. Here are the findings, most severe first:

Correctness bugs (real, fix these):

backend/app/core/risk/circuit_breaker.py — record_closed_trade() mutates _daily_realized_pnl, _halted_today, and per-strategy consecutive_losses with no lock. Two trades closing concurrently (e.g. price-hit close + stale-sweep close firing near-simultaneously) can lose a PnL update or skip a halt. Ironic since this commit's whole point was closing a race condition — it introduced a new one in the safety mechanism that replaced it.
backend/app/core/execution/order_manager.py close_position() — every failure path (trade not found, wrong status, broker close fails after retries, broker rejects the close) returns without calling CircuitBreaker.record_closed_trade(). A losing trade that fails to close cleanly never reaches the halt logic, and is left stuck at CLOSING with no further attempts.
backend/app/core/execution/position_monitor.py — asyncio.create_task(self._close(...)) (price-hit path and stale-sweep path) is fire-and-forget; stop() cancels the sweep loop but never tracks/awaits/cancels these spawned tasks, so they can touch torn-down DB resources during shutdown.
position_monitor.py trade_executed handler — requires entry_price/stop/take-profit all present or it silently skips tracking (no monitoring, no alert escalation, no reconciliation sweep to catch an open-but-untracked trade later). Plausible gap on partial fills or payload issues, not confirmed to trigger in normal operation.
Cleanup / efficiency (lower priority):

order_manager.py — DB query (get_active_trades) runs inside _submission_lock on every order, serializing all submissions through one DB round-trip. Fine at current scale; a hung DB connection would freeze all order flow.
min_separation_pct config/validation/schema is copy-pasted near-identically across moving_average.py, stock_trend.py, bull_bear.py instead of centralized.
order_manager.py retry/backoff re-implements a pattern the IBKR client already has for reconnects — two independent retry layers against different failure modes.
scheduler.py fetches required_extra_symbols bars sequentially per symbol instead of asyncio.gather.
circuit_breaker.py — a break-even close (pnl == 0) resets consecutive_losses to 0, treating break-even as a win (debatable, not necessarily wrong).
filters.py::min_separation_ok returns True (passes the filter) when the slower MA is <= 0 — silently accepts a degenerate value instead of flagging it.
composite.py — combined signal can inherit None stop_loss_price if all agreeing sub-signals lack one, wasting a validate-and-reject round trip at RiskManager Gate 1.
risk/monitor.py check_exposure() counts only OPEN trades while OrderManager's actual cap counts PENDING+SUBMITTED+OPEN — dashboard can under-report real exposure vs. the enforced cap.
composite.py majority-vote counts actions then re-filters in a second pass instead of building the result in one loop.
Now let me set up the todo list to track the hardening work.

Update Todos

Fix CircuitBreaker.record_closed_trade() race condition (no lock on _daily_realized_pnl/_halted_today/consecutive_losses)

Make close_position() notify CircuitBreaker on every failure path (not-found, wrong status, broker close failure/rejection), and resolve stuck CLOSING trades

Track and cancel/await PositionMonitor's fire-and-forget _close() tasks during stop() shutdown

Add reconciliation/alerting for trade_executed events that are silently skipped (missing entry_price/stop/take-profit)

Review DB query inside OrderManager._submission_lock for timeout/contention risk under load

Centralize min_separation_pct config/validation/schema shared by moving_average/stock_trend/bull_bear strategies

Reconcile OrderManager's duplicate retry/backoff with IBKR client's existing reconnect backoff

Parallelize scheduler's sequential required_extra_symbols bar fetches with asyncio.gather

Decide whether break-even (pnl==0) closes should reset CircuitBreaker consecutive_losses streak

Fix filters.min_separation_ok to not silently pass when the slower MA is <= 0

Prevent composite.py from combining signals into a None stop_loss_price

Align RiskMonitor.check_exposure() to count PENDING+SUBMITTED+OPEN like OrderManager's actual cap

Simplify composite.py majority-vote to single-pass count+filter