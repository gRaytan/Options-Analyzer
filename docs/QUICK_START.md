# Quick Start Guide - Options Analyzer

## 🎯 Goal
Get the Options Analyzer running on your local machine and analyze your first set of positions.

## ⚡ 5-Minute Setup (Once Development is Complete)

### Step 1: Install Python
```bash
# Check if Python 3.10+ is installed
python3 --version

# If not installed, download from python.org
```

### Step 2: Set Up Project
```bash
# Navigate to project directory
cd /Users/gilr/IdeaProjects/OptionsAnalyzer

# Create virtual environment
python3 -m venv venv

# Activate virtual environment
source venv/bin/activate  # On Mac/Linux
# OR
venv\Scripts\activate     # On Windows

# Install dependencies
pip install -r requirements.txt
```

### Step 3: Export Data from Interactive Brokers
```bash
# In TWS (Trader Workstation):
# 1. Click Portfolio tab
# 2. Right-click → Export to File
# 3. Save as CSV (e.g., ib_positions.csv)
```

### Step 4: Run the Application
```bash
# Start Flask server
python src/app.py

# Open browser to:
# http://localhost:5000
```

### Step 5: Upload & Analyze
```
1. Click "Upload Positions" button
2. Select your CSV file from IB
3. Wait for processing (< 2 seconds)
4. View dashboard, positions, and recommendations
```

## 📋 Current Status (As of Now)

### ✅ Completed
- [x] Project structure created
- [x] Documentation written (PRD, Architecture, Implementation Plan)
- [x] Requirements defined
- [x] Configuration files set up
- [x] Directory structure created

### 🔜 To Be Implemented (Next Steps)
- [ ] Data models (Position, Portfolio, Recommendation)
- [ ] Flask application skeleton
- [ ] CSV parser for IB exports
- [ ] Greeks calculator
- [ ] Margin calculator
- [ ] Recommendation engine
- [ ] Web UI (Dashboard, Positions, Recommendations)
- [ ] Tests

## 🚦 Before You Can Use It

### What You Need to Provide:
1. **Sample IB CSV Export**
   - Export your positions from TWS
   - Can anonymize symbols if needed
   - We need this to build the parser

2. **Configuration Preferences**
   - Profit target percentage (default: 50%)
   - Delta threshold for risk (default: 0.6)
   - Margin warning level (default: 80%)

3. **Validation Data**
   - IB's margin calculations for a few positions
   - To validate our margin calculator accuracy

## 📖 Documentation Guide

### New to the Project?
Start here: **[PROJECT_SUMMARY.md](PROJECT_SUMMARY.md)**

### Want to Understand Features?
Read: **[PRD.md](PRD.md)** (Product Requirements)

### Need Technical Details?
Read: **[ARCHITECTURE.md](ARCHITECTURE.md)**

### How to Export from IB?
Read: **[IB_EXPORT_GUIDE.md](IB_EXPORT_GUIDE.md)**

### Want to Know Development Timeline?
Read: **[IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md)**

## 🔧 Development Setup (For Contributing)

### Install Development Tools
```bash
# Install all dependencies including dev tools
pip install -r requirements.txt

# Install pre-commit hooks (optional)
pip install pre-commit
pre-commit install
```

### Run Tests
```bash
# Run all tests
pytest

# Run with coverage
pytest --cov=src tests/

# Run specific test file
pytest tests/test_parser.py
```

### Code Quality
```bash
# Format code
black src/

# Lint code
pylint src/

# Type checking (if we add type hints)
mypy src/
```

## 🎓 Understanding the Workflow

### User Workflow (Once Built)
```
1. Export positions from IB (CSV)
   ↓
2. Upload CSV to Options Analyzer
   ↓
3. System parses and validates data
   ↓
4. System calculates Greeks and Margin
   ↓
5. System analyzes risks
   ↓
6. System generates recommendations
   ↓
7. User views dashboard and positions
   ↓
8. User reviews recommendations
   ↓
9. User takes action in IB
```

### Data Flow
```
IB CSV → Parser → Validator → Storage (JSON)
                                  ↓
                    Greeks Calculator ← Storage
                    Margin Calculator ← Storage
                                  ↓
                         Risk Analyzer
                                  ↓
                    Recommendation Engine
                                  ↓
                         Web UI (Flask)
                                  ↓
                            User Browser
```

## 🐛 Troubleshooting (Future)

### Application Won't Start
```bash
# Check Python version
python3 --version  # Should be 3.10+

# Check virtual environment is activated
which python  # Should point to venv/bin/python

# Reinstall dependencies
pip install -r requirements.txt --force-reinstall
```

### CSV Upload Fails
- Check file is actually CSV (not Excel)
- Verify file size < 10MB
- Ensure file has required columns
- Check error message in UI

### Calculations Seem Wrong
- Compare with IB's values
- Check configuration in data/config/settings.json
- Verify underlying price is correct
- Check expiration date format

## 📞 Getting Help

### Common Questions

**Q: When will the tool be ready to use?**
A: Following the implementation plan, approximately 4-5 weeks from start of development.

**Q: Can I use this with paper trading account?**
A: Yes! Works with both live and paper trading accounts.

**Q: Will this execute trades automatically?**
A: No. This is an analysis tool only. You must execute trades manually in IB.

**Q: How often should I update the data?**
A: For Phase 1 (MVP), update manually as needed (daily, weekly, etc.). Phase 2 will add real-time updates.

**Q: Can I use this with other brokers?**
A: Phase 1 is IB-specific. Future phases could support other brokers if they provide similar data exports.

## 🚀 Next Steps

### For You (Right Now):
1. ✅ Review all documentation
2. ⏳ Export sample CSV from Interactive Brokers
3. ⏳ Answer configuration questions (profit target %, etc.)
4. ⏳ Decide when to start development

### For Development (After You Provide CSV):
1. Build data models
2. Create Flask skeleton
3. Implement CSV parser
4. Build calculators
5. Create web UI
6. Test and validate

## 📊 Expected Results

Once complete, you'll be able to:
- ✅ Upload 500+ positions in seconds
- ✅ See all positions with Delta, IV, Margin, P&L
- ✅ Sort and filter by any metric
- ✅ Get flagged on high-risk positions
- ✅ Receive prioritized recommendations
- ✅ Monitor portfolio-level Greeks and margin
- ✅ Make data-driven decisions on position management

---

**Ready to begin?** Provide a sample IB CSV export and we'll start building! 🎉

