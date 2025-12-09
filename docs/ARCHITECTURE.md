# Technical Architecture Document
## Options Analyzer Tool

### 1. System Overview

The Options Analyzer is a web-based application built with Python that processes options position data, performs analysis, and provides recommendations through a simple web interface.

```
┌─────────────────┐
│  Interactive    │
│    Brokers      │
└────────┬────────┘
         │ Manual Export (CSV)
         ▼
┌─────────────────────────────────────────────────────────┐
│                   Options Analyzer                       │
│                                                          │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐  │
│  │   Data       │  │   Analysis   │  │     Web      │  │
│  │   Import     │─▶│   Engine     │─▶│   Interface  │  │
│  │   Layer      │  │              │  │   (Flask)    │  │
│  └──────────────┘  └──────────────┘  └──────────────┘  │
│         │                  │                  │         │
│         ▼                  ▼                  ▼         │
│  ┌─────────────────────────────────────────────────┐   │
│  │         File Storage (JSON/CSV)                 │   │
│  └─────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────┘
         │
         ▼
┌─────────────────┐
│   User Browser  │
└─────────────────┘
```

### 2. Architecture Layers

#### 2.1 Data Import Layer
**Responsibility**: Parse and validate position data from IB exports

**Components**:
- `IBDataParser`: Parse CSV/Excel files from IB
- `DataValidator`: Validate data completeness and format
- `PositionNormalizer`: Convert IB format to internal data model

**Key Operations**:
- Read uploaded files
- Parse position records
- Validate required fields
- Transform to standardized format
- Store in local file system

#### 2.2 Analysis Engine
**Responsibility**: Calculate metrics, margins, and generate recommendations

**Components**:
- `MarginCalculator`: Calculate Reg-T and Portfolio Margin
- `GreeksCalculator`: Calculate/retrieve option Greeks
- `RiskAnalyzer`: Identify high-risk positions
- `RecommendationEngine`: Generate actionable recommendations
- `PortfolioAggregator`: Calculate portfolio-level metrics

**Key Operations**:
- Calculate margin requirements per position
- Compute portfolio Greeks
- Identify positions meeting closure criteria
- Generate prioritized recommendations
- Aggregate portfolio statistics

#### 2.3 Web Interface Layer
**Responsibility**: Serve web UI and handle user interactions

**Components**:
- `FlaskApp`: Web server and routing
- `APIEndpoints`: REST endpoints for data operations
- `TemplateRenderer`: HTML template rendering
- `StaticAssets`: CSS/JS for UI

**Key Operations**:
- Serve web pages
- Handle file uploads
- Provide data via JSON API
- Render position tables
- Display recommendations

#### 2.4 Storage Layer
**Responsibility**: Persist position data and configuration

**Components**:
- `FileStorage`: JSON/CSV file operations
- `ConfigManager`: User preferences and settings

**Key Operations**:
- Save/load position data
- Store user configuration
- Cache calculated metrics

### 3. Data Models

#### 3.1 Position Model
```python
{
    "id": "unique_position_id",
    "symbol": "AAPL",
    "option_type": "CALL" | "PUT",
    "strike": 150.0,
    "expiration": "2024-01-19",
    "quantity": -10,  # negative for short
    "entry_price": 2.50,
    "current_price": 1.80,
    "underlying_price": 148.50,
    "delta": 0.35,
    "gamma": 0.05,
    "theta": -0.02,
    "vega": 0.15,
    "implied_volatility": 0.25,
    "margin_requirement": {
        "reg_t": 5000.0,
        "portfolio": 3500.0
    },
    "pnl": {
        "absolute": 700.0,
        "percentage": 28.0
    },
    "days_to_expiration": 45,
    "probability_of_profit": 0.68,
    "flags": ["HIGH_DELTA", "PROFIT_TARGET_MET"]
}
```

#### 3.2 Portfolio Summary Model
```python
{
    "total_positions": 523,
    "total_pnl": 15420.50,
    "total_pnl_percentage": 12.3,
    "margin_used": 125000.0,
    "margin_available": 75000.0,
    "margin_utilization": 0.625,
    "portfolio_greeks": {
        "delta": -150.5,
        "gamma": 25.3,
        "theta": 450.2,
        "vega": -320.1
    },
    "flagged_positions": 15,
    "recommendations_count": 8
}
```

#### 3.3 Recommendation Model
```python
{
    "id": "rec_001",
    "position_id": "pos_123",
    "action": "CLOSE",
    "priority": "HIGH" | "MEDIUM" | "LOW",
    "reason": "High assignment risk - Delta 0.72",
    "details": {
        "current_delta": 0.72,
        "threshold": 0.60,
        "days_to_expiration": 3
    },
    "estimated_impact": {
        "margin_freed": 5000.0,
        "pnl_if_closed": 350.0
    }
}
```

### 4. Technology Stack

#### 4.1 Backend
- **Language**: Python 3.10+
- **Web Framework**: Flask 3.0+ (lightweight, fast setup)
- **Data Processing**: Pandas (CSV/Excel parsing, data manipulation)
- **Options Calculations**: 
  - `py_vollib` (Black-Scholes, IV calculations)
  - Custom margin calculators
- **File I/O**: JSON (standard library), CSV (pandas)

#### 4.2 Frontend
- **HTML/CSS**: Bootstrap 5 (responsive tables, simple styling)
- **JavaScript**: Vanilla JS or jQuery (sorting, filtering, AJAX)
- **Data Tables**: DataTables.js (advanced table features)

#### 4.3 Development Tools
- **Package Management**: pip, requirements.txt
- **Testing**: pytest
- **Code Quality**: black (formatting), pylint (linting)

### 5. Key Algorithms

#### 5.1 Reg-T Margin Calculation
```
For short naked calls:
  Margin = MAX(
    20% * underlying_price * quantity * 100 - OTM_amount,
    10% * underlying_price * quantity * 100
  ) + option_value

For short naked puts:
  Margin = MAX(
    20% * strike * quantity * 100 - OTM_amount,
    10% * strike * quantity * 100
  ) + option_value

For spreads:
  Margin = MAX(
    (strike_short - strike_long) * quantity * 100,
    0
  ) - net_credit
```

#### 5.2 Portfolio Margin (Simplified)
```
Use risk-based scenarios:
- Calculate P&L for underlying price moves: ±15%, ±10%, ±5%
- Calculate P&L for volatility changes: ±25%
- Margin = MAX(worst_case_loss across all scenarios) * 1.1
```

#### 5.3 Recommendation Scoring
```
Priority Score = 
  (assignment_risk_weight * delta_score) +
  (margin_pressure_weight * margin_utilization_score) +
  (profit_target_weight * profit_percentage_score) +
  (time_urgency_weight * dte_score)

Sort recommendations by priority score (descending)
```

### 6. File Structure
```
OptionsAnalyzer/
├── src/
│   ├── __init__.py
│   ├── app.py                 # Flask application entry point
│   ├── data/
│   │   ├── __init__.py
│   │   ├── parser.py          # IB data parsing
│   │   ├── validator.py       # Data validation
│   │   └── storage.py         # File storage operations
│   ├── analysis/
│   │   ├── __init__.py
│   │   ├── margin.py          # Margin calculations
│   │   ├── greeks.py          # Greeks calculations
│   │   ├── risk.py            # Risk analysis
│   │   └── recommendations.py # Recommendation engine
│   ├── models/
│   │   ├── __init__.py
│   │   ├── position.py        # Position data model
│   │   └── portfolio.py       # Portfolio data model
│   └── web/
│       ├── __init__.py
│       ├── routes.py          # Flask routes
│       └── api.py             # API endpoints
├── templates/
│   ├── base.html
│   ├── dashboard.html
│   ├── positions.html
│   └── recommendations.html
├── static/
│   ├── css/
│   │   └── style.css
│   └── js/
│       └── app.js
├── data/                      # Local data storage
│   ├── positions/
│   └── config/
├── tests/
│   ├── test_parser.py
│   ├── test_margin.py
│   └── test_recommendations.py
├── docs/
│   ├── PRD.md
│   ├── ARCHITECTURE.md
│   └── IB_EXPORT_GUIDE.md
├── requirements.txt
├── README.md
└── .gitignore
```

### 7. API Endpoints

```
GET  /                          # Dashboard home
GET  /positions                 # View all positions
POST /upload                    # Upload IB export file
GET  /api/positions             # Get positions JSON
GET  /api/portfolio-summary     # Get portfolio summary JSON
GET  /api/recommendations       # Get recommendations JSON
POST /api/refresh               # Recalculate all metrics
GET  /api/position/<id>         # Get single position details
```

### 8. Security Considerations

- **Phase 1 (Local deployment)**:
  - No authentication required (single user, local machine)
  - File uploads validated for type and size
  - No sensitive data stored in logs

- **Phase 2 (Future - Server deployment)**:
  - User authentication (JWT tokens)
  - HTTPS only
  - Rate limiting on API endpoints
  - Encrypted data storage
  - Session management

### 9. Deployment Architecture

#### Phase 1: Local Development
```
User's Machine:
  - Python 3.10+ installed
  - Flask app running on localhost:5000
  - Data stored in local filesystem
  - Access via http://localhost:5000
```

#### Phase 2: Server Deployment (Future)
```
Cloud Server (AWS/GCP/Azure):
  - Docker container with Flask app
  - PostgreSQL database
  - Nginx reverse proxy
  - SSL certificate
  - Scheduled jobs for data refresh
```

### 10. Performance Optimization

- **Caching**: Cache calculated metrics to avoid recalculation
- **Lazy Loading**: Load position details on-demand
- **Pagination**: Display positions in pages (100 per page)
- **Indexing**: Index positions by symbol, expiration for fast filtering
- **Async Processing**: Use background tasks for heavy calculations (future)

### 11. Error Handling

- **Data Import Errors**: Clear messages for invalid CSV format
- **Calculation Errors**: Fallback to default values, log warnings
- **API Errors**: Return proper HTTP status codes with error details
- **UI Errors**: Display user-friendly error messages

### 12. Testing Strategy

- **Unit Tests**: Test individual calculators and parsers
- **Integration Tests**: Test end-to-end data flow
- **Validation Tests**: Compare margin calculations with IB's values
- **Performance Tests**: Test with 1000+ positions
- **Manual Testing**: Verify UI functionality and usability

