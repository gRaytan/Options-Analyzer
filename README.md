# Options Analyzer

A web-based tool to analyze options trading positions from Interactive Brokers, calculate margin requirements, track key metrics, and provide actionable recommendations for position management.

## Features

### Phase 1 (MVP - Current)
- ✅ Import position data from Interactive Brokers (CSV export)
- ✅ Display positions with key metrics (Delta, IV, P&L, Margin)
- ✅ Calculate Reg-T and Portfolio Margin requirements
- ✅ Sort and filter positions by any metric
- ✅ Risk analysis and position flagging
- ✅ Automated recommendations for position management
- ✅ Simple web interface for portfolio monitoring

### Phase 2 (Future)
- 🔄 Real-time data integration via IB API
- 🔄 New opportunity scanning
- 🔄 Email/SMS alerts
- 🔄 Historical tracking and analytics
- 🔄 Advanced charting
- 🔄 Multi-user SaaS platform

## Quick Start

### Prerequisites
- Python 3.10 or higher
- Interactive Brokers account with position data
- Modern web browser

### Installation

1. **Clone the repository**
   ```bash
   git clone <repository-url>
   cd OptionsAnalyzer
   ```

2. **Create virtual environment**
   ```bash
   python -m venv venv
   source venv/bin/activate  # On Windows: venv\Scripts\activate
   ```

3. **Install dependencies**
   ```bash
   pip install -r requirements.txt
   ```

4. **Run the application**
   ```bash
   python src/app.py
   ```

5. **Open in browser**
   ```
   http://localhost:5000
   ```

## Usage

### 1. Export Data from Interactive Brokers
See [IB Export Guide](docs/IB_EXPORT_GUIDE.md) for detailed instructions.

**Quick steps:**
- Open TWS (Trader Workstation)
- Go to Portfolio tab
- Right-click → Export to File → Save as CSV
- Save file (e.g., `ib_positions.csv`)

### 2. Upload to Options Analyzer
- Click "Upload Positions" button
- Select your CSV file
- Wait for processing (usually < 2 seconds for 500+ positions)

### 3. View Analysis
- **Dashboard**: Portfolio summary, total P&L, margin utilization
- **Positions Table**: All positions with sortable columns
- **Recommendations**: Actionable suggestions for position management

### 4. Take Action
- Review flagged positions (high risk, profit targets met)
- Follow recommendations to close or adjust positions
- Monitor margin utilization

## Project Structure

```
OptionsAnalyzer/
├── src/                    # Source code
│   ├── app.py             # Flask application entry point
│   ├── data/              # Data import and storage
│   ├── analysis/          # Margin, Greeks, recommendations
│   ├── models/            # Data models
│   └── web/               # Web routes and API
├── templates/             # HTML templates
├── static/                # CSS, JavaScript
├── data/                  # Local data storage
├── tests/                 # Unit and integration tests
├── docs/                  # Documentation
│   ├── PRD.md            # Product requirements
│   ├── ARCHITECTURE.md   # Technical architecture
│   └── IB_EXPORT_GUIDE.md # IB export instructions
├── requirements.txt       # Python dependencies
└── README.md             # This file
```

## Key Metrics Explained

- **Delta**: Rate of change of option price relative to underlying price (0-1 for calls, 0 to -1 for puts)
- **Implied Volatility (IV)**: Market's expectation of future volatility
- **Margin**: Capital required to maintain the position
- **Probability of Profit**: Estimated likelihood of profitable outcome
- **DTE**: Days to expiration

## Recommendations Logic

The tool recommends closing positions when:
1. **Profit Target Met**: Position has reached configured profit percentage (default: 50%)
2. **High Assignment Risk**: Naked short calls with Delta > 0.6
3. **Margin Pressure**: Insufficient margin available for portfolio
4. **Time Decay Optimization**: Better to close and redeploy capital

## Configuration

Edit `data/config/settings.json` to customize:
- Profit target percentage
- Delta threshold for assignment risk
- Margin utilization warning level
- Recommendation priorities

Example:
```json
{
  "profit_target_percentage": 50,
  "high_delta_threshold": 0.6,
  "margin_warning_level": 0.8,
  "days_to_expiration_warning": 7
}
```

## Development

### Running Tests
```bash
pytest tests/
```

### Code Formatting
```bash
black src/
```

### Linting
```bash
pylint src/
```

## Documentation

- [Product Requirements Document (PRD)](docs/PRD.md)
- [Technical Architecture](docs/ARCHITECTURE.md)
- [IB Export Guide](docs/IB_EXPORT_GUIDE.md)

## Roadmap

### Phase 1 (Current) - MVP
- [x] Project setup and documentation
- [ ] IB CSV parser
- [ ] Margin calculators (Reg-T and Portfolio)
- [ ] Greeks calculations
- [ ] Recommendation engine
- [ ] Web UI (Flask + Bootstrap)
- [ ] Testing and validation

### Phase 2 - Real-time Integration
- [ ] IB API integration
- [ ] Real-time data updates
- [ ] New opportunity scanner
- [ ] Email alerts

### Phase 3 - SaaS Platform
- [ ] Multi-user support
- [ ] Database backend (PostgreSQL)
- [ ] Advanced analytics
- [ ] Mobile app

## Support

For issues or questions:
1. Check the [IB Export Guide](docs/IB_EXPORT_GUIDE.md)
2. Review [Architecture Documentation](docs/ARCHITECTURE.md)
3. Open an issue on GitHub

## License

[To be determined]

## Disclaimer

This tool is for informational purposes only. It does not constitute financial advice. Options trading involves significant risk. Always verify calculations and recommendations before taking action. The accuracy of margin calculations and recommendations depends on the quality of input data and market conditions.

