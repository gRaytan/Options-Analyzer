# Product Requirements Document (PRD)
## Options Analyzer Tool

### 1. Overview
A web-based tool to analyze options trading positions from Interactive Brokers, calculate margin requirements, track key metrics, and provide actionable recommendations for position management.

### 2. Target Users
- **Primary**: Individual options trader managing 500+ positions
- **Future**: SaaS platform for multiple users

### 3. Core Objectives
1. Simplify options portfolio monitoring and analysis
2. Automate margin calculation and risk assessment
3. Provide data-driven recommendations for position management
4. Enable efficient sorting and filtering of large position sets

### 4. Product Scope

#### Phase 1 (MVP) - Current Focus
**In Scope:**
- Import position data from Interactive Brokers (manual export)
- Display all positions with key metrics
- Calculate margin requirements (Reg-T and Portfolio Margin)
- Sort and filter positions by metrics (Delta, IV, P&L, Margin)
- Generate recommendations for position closure
- Simple web UI for viewing and interacting with data

**Out of Scope (Future Phases):**
- Real-time data streaming
- New opportunity scanning
- Email/SMS alerts
- Multi-user support
- Historical position tracking
- Advanced charting and visualizations

### 5. Functional Requirements

#### 5.1 Data Import
- **FR-1.1**: Support manual CSV/Excel export from Interactive Brokers
- **FR-1.2**: Parse position data including: symbol, quantity, strike, expiration, option type, entry price
- **FR-1.3**: Validate imported data for completeness and accuracy
- **FR-1.4**: Support re-import to refresh position data

#### 5.2 Position Display
- **FR-2.1**: Display all positions in a sortable table
- **FR-2.2**: Show key metrics per position:
  - Symbol and option details (strike, expiration, type)
  - Quantity
  - Current P&L (absolute and percentage)
  - Delta
  - Implied Volatility (IV)
  - Margin requirement
  - Days to Expiration (DTE)
  - Probability of Profit
- **FR-2.3**: Support filtering by symbol, expiration date, option type
- **FR-2.4**: Support sorting by any metric column

#### 5.3 Margin Calculation
- **FR-3.1**: Calculate Reg-T margin for each position
- **FR-3.2**: Calculate Portfolio Margin for each position
- **FR-3.3**: Display total margin used and available buying power
- **FR-3.4**: Show margin utilization percentage
- **FR-3.5**: Support both naked options and spreads

#### 5.4 Risk Analysis
- **FR-4.1**: Flag positions with high assignment risk:
  - Naked short calls with Delta > 0.6
  - Other configurable risk thresholds
- **FR-4.2**: Calculate and display portfolio-level Greeks (total Delta, Gamma, Theta, Vega)
- **FR-4.3**: Show margin utilization warnings

#### 5.5 Recommendations Engine
- **FR-5.1**: Recommend positions to close based on:
  - Profit target reached (configurable %)
  - High assignment risk (Delta > 0.6 for naked calls)
  - Insufficient margin available
- **FR-5.2**: Prioritize recommendations by urgency/impact
- **FR-5.3**: Display rationale for each recommendation

#### 5.6 User Interface
- **FR-6.1**: Web-based interface accessible via browser
- **FR-6.2**: Simple, responsive table layout
- **FR-6.3**: File upload interface for data import
- **FR-6.4**: Dashboard showing portfolio summary:
  - Total positions
  - Total P&L
  - Margin utilization
  - Number of flagged positions
- **FR-6.5**: Recommendations panel with actionable items

### 6. Non-Functional Requirements

#### 6.1 Performance
- **NFR-1.1**: Load and display 500+ positions within 2 seconds
- **NFR-1.2**: Support up to 1000 positions without performance degradation

#### 6.2 Usability
- **NFR-2.1**: Intuitive interface requiring minimal training
- **NFR-2.2**: Clear error messages for invalid data imports

#### 6.3 Reliability
- **NFR-3.1**: Accurate margin calculations (validated against IB's calculations)
- **NFR-3.2**: Data validation to prevent incorrect analysis

#### 6.4 Deployment
- **NFR-4.1**: Run locally on user's machine (Phase 1)
- **NFR-4.2**: On-demand execution (not continuous)
- **NFR-4.3**: Easy setup with minimal dependencies

### 7. Technical Constraints
- **Language**: Python
- **UI Framework**: Flask or FastAPI with simple HTML/JS
- **Data Storage**: File-based (JSON/CSV) for speed of development
- **Data Source**: Manual exports from Interactive Brokers

### 8. Success Metrics
- Successfully import and analyze 500+ positions
- Accurate margin calculations (within 5% of IB's calculations)
- Generate actionable recommendations for at least 80% of at-risk positions
- Page load time < 2 seconds for full portfolio

### 9. Future Enhancements (Phase 2+)
- Real-time data integration via IB API
- New opportunity scanning and suggestions
- Email/SMS alerts for critical events
- Historical position tracking and performance analytics
- Advanced charting and visualizations
- Multi-user support for SaaS model
- Continuous monitoring mode
- Database backend (PostgreSQL)
- Mobile-responsive design improvements

### 10. Open Questions
1. What is the exact format of IB's position export? (CSV structure)
2. Where to source real-time Greeks and IV data for positions?
3. Specific profit target percentage for close recommendations?
4. Should the tool support options on futures or only equity options?

### 11. Assumptions
- User has active Interactive Brokers account with API access
- User can export position data from IB TWS or IB Gateway
- Greeks (Delta, Gamma, Theta, Vega) are available in IB export or can be calculated
- User understands options trading terminology and metrics

