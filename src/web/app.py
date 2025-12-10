"""Flask web application for Options Analyzer."""
import os
from flask import Flask, render_template, request, jsonify, redirect, url_for, flash, session
from werkzeug.utils import secure_filename

from src.data.parser import IBActivityStatementParser
from src.data.price_fetcher import update_portfolio_prices
from src.data.storage import DataStorage
from src.analysis.greeks import calculate_portfolio_greeks
from src.analysis.margin import calculate_portfolio_margin
from src.analysis.risk import analyze_portfolio_risk
from src.models.portfolio import Portfolio


def create_app(config=None):
    """Create and configure the Flask application."""
    app = Flask(__name__,
                template_folder='../../templates',
                static_folder='../../static')

    app.secret_key = os.environ.get('SECRET_KEY', 'dev-secret-key-change-in-production')
    app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB max file size

    # Initialize storage
    storage = DataStorage()

    # Store in app context - now supports multiple portfolios
    app.storage = storage
    app.portfolios = {}  # Dict of portfolio_id -> portfolio
    app.risk_analyses = {}  # Dict of portfolio_id -> risk_analysis

    def get_active_portfolio():
        """Get the currently selected portfolio based on session filter."""
        portfolio_filter = session.get('portfolio_filter', 'all')

        if not app.portfolios:
            return None, None

        if portfolio_filter == 'all':
            # Combine all portfolios - aggregate positions, don't replace
            combined = Portfolio()
            combined.options = []
            combined.stocks = []
            combined.trades = []
            combined.net_asset_value = 0
            combined.cash_balance = 0
            account_names = []

            for pid, p in app.portfolios.items():
                # Combine all options positions
                combined.options.extend(p.options)
                # Combine all stock positions
                combined.stocks.extend(p.stocks)
                # Combine all trades (deduplicate by key)
                existing_trades = {
                    (t.symbol, t.trade_date, t.quantity, t.price): t
                    for t in combined.trades
                }
                for trade in p.trades:
                    key = (trade.symbol, trade.trade_date, trade.quantity, trade.price)
                    if key not in existing_trades:
                        combined.trades.append(trade)
                        existing_trades[key] = trade

                # Sum up NAV and cash
                if p.net_asset_value:
                    combined.net_asset_value += p.net_asset_value
                if p.cash_balance:
                    combined.cash_balance += p.cash_balance

                # Collect account names
                if p.account_name:
                    account_names.append(p.account_name)

            combined.account_name = " + ".join(account_names) if account_names else "Combined"

            # Recalculate for combined
            if combined.options:
                calculate_portfolio_greeks(combined)
                calculate_portfolio_margin(combined, margin_type="reg_t")

            config = storage.get_config()
            risk_analysis = analyze_portfolio_risk(combined, config)
            return combined, risk_analysis
        else:
            # Return specific portfolio
            portfolio = app.portfolios.get(portfolio_filter)
            risk_analysis = app.risk_analyses.get(portfolio_filter)
            return portfolio, risk_analysis

    app.get_active_portfolio = get_active_portfolio
    
    @app.route('/')
    def index():
        """Dashboard page."""
        portfolio, risk_analysis = get_active_portfolio()

        if not app.portfolios:
            return render_template('upload.html')

        return render_template('dashboard.html',
                             portfolio=portfolio,
                             risk_analysis=risk_analysis,
                             portfolios=app.portfolios,
                             portfolio_filter=session.get('portfolio_filter', 'all'))

    @app.route('/set_portfolio/<portfolio_id>')
    def set_portfolio(portfolio_id):
        """Set the active portfolio filter."""
        if portfolio_id == 'all' or portfolio_id in app.portfolios:
            session['portfolio_filter'] = portfolio_id
        return redirect(request.referrer or url_for('index'))

    @app.route('/remove_portfolio/<portfolio_id>')
    def remove_portfolio(portfolio_id):
        """Remove a portfolio."""
        if portfolio_id in app.portfolios:
            del app.portfolios[portfolio_id]
            if portfolio_id in app.risk_analyses:
                del app.risk_analyses[portfolio_id]
            # Reset filter if removed portfolio was selected
            if session.get('portfolio_filter') == portfolio_id:
                session['portfolio_filter'] = 'all'
            flash(f'Portfolio "{portfolio_id}" removed', 'success')
        return redirect(url_for('upload'))

    @app.route('/upload', methods=['GET', 'POST'])
    def upload():
        """Handle file upload - each file becomes a separate portfolio."""
        if request.method == 'POST':
            if 'file' not in request.files:
                flash('No file selected', 'error')
                return redirect(request.url)

            files = request.files.getlist('file')
            if not files or all(f.filename == '' for f in files):
                flash('No file selected', 'error')
                return redirect(request.url)

            try:
                parser = IBActivityStatementParser()
                files_processed = 0

                for file in files:
                    if file.filename == '':
                        continue

                    content = file.read().decode('utf-8-sig')
                    portfolio = parser.parse_content(content)

                    # Generate portfolio ID from filename or account
                    filename = secure_filename(file.filename)
                    base_name = os.path.splitext(filename)[0]
                    portfolio_id = portfolio.account_name or portfolio.account_id or base_name

                    # Ensure unique ID
                    original_id = portfolio_id
                    counter = 1
                    while portfolio_id in app.portfolios:
                        portfolio_id = f"{original_id}_{counter}"
                        counter += 1

                    # Fetch missing prices
                    update_portfolio_prices(portfolio)

                    # Calculate Greeks
                    calculate_portfolio_greeks(portfolio)

                    # Calculate Margin
                    calculate_portfolio_margin(portfolio, margin_type="reg_t")

                    # Analyze Risk
                    config = storage.get_config()
                    risk_analysis = analyze_portfolio_risk(portfolio, config)

                    # Store in app context
                    app.portfolios[portfolio_id] = portfolio
                    app.risk_analyses[portfolio_id] = risk_analysis
                    files_processed += 1

                if files_processed == 0:
                    flash('No valid files found', 'error')
                    return redirect(request.url)

                flash(f'Loaded {files_processed} portfolio(s)', 'success')
                return redirect(url_for('index'))

            except Exception as e:
                flash(f'Error parsing file: {str(e)}', 'error')
                return redirect(request.url)

        return render_template('upload.html',
                             portfolios=app.portfolios,
                             portfolio_filter=session.get('portfolio_filter', 'all'))
    
    @app.route('/positions')
    def positions():
        """Positions table page."""
        portfolio, _ = get_active_portfolio()
        if not app.portfolios:
            return redirect(url_for('upload'))

        # Get filter parameters
        filter_type = request.args.get('type', 'all')  # all, calls, puts
        filter_risk = request.args.get('risk', 'all')  # all, high, flagged
        sort_by = request.args.get('sort', 'underlying')
        sort_dir = request.args.get('dir', 'asc')  # asc or desc

        options = portfolio.options if portfolio else []

        # Apply filters
        if filter_type == 'calls':
            options = [o for o in options if o.is_call]
        elif filter_type == 'puts':
            options = [o for o in options if o.is_put]

        if filter_risk == 'high':
            options = [o for o in options if 'HIGH_DELTA_SHORT_CALL' in o.flags or 'LARGE_LOSS' in o.flags]
        elif filter_risk == 'flagged':
            options = [o for o in options if o.flags]

        # Sort configuration: column -> (key_func, default_reverse)
        sort_config = {
            'underlying': (lambda x: x.underlying.lower(), False),
            'strike': (lambda x: x.strike, False),
            'expiration': (lambda x: x.expiration or datetime.min, False),
            'dte': (lambda x: x.days_to_expiration, False),
            'qty': (lambda x: x.quantity, False),
            'stock_price': (lambda x: x.underlying_price, False),
            'option_price': (lambda x: x.current_price, False),
            'delta': (lambda x: x.delta, False),
            'iv': (lambda x: x.implied_volatility, False),
            'pnl': (lambda x: x.unrealized_pnl, True),  # Default descending (best first)
            'margin': (lambda x: x.margin_requirement, True),
        }

        if sort_by in sort_config:
            key_func, default_reverse = sort_config[sort_by]
            reverse = (sort_dir == 'desc')
            options = sorted(options, key=key_func, reverse=reverse)

        return render_template('positions.html',
                             options=options,
                             portfolio=portfolio,
                             filter_type=filter_type,
                             filter_risk=filter_risk,
                             sort_by=sort_by,
                             sort_dir=sort_dir,
                             portfolios=app.portfolios,
                             portfolio_filter=session.get('portfolio_filter', 'all'))
    
    @app.route('/recommendations')
    def recommendations():
        """Recommendations page."""
        portfolio, risk_analysis = get_active_portfolio()

        if not app.portfolios:
            return redirect(url_for('upload'))

        return render_template('recommendations.html',
                             portfolio=portfolio,
                             risk_analysis=risk_analysis,
                             portfolios=app.portfolios,
                             portfolio_filter=session.get('portfolio_filter', 'all'))

    @app.route('/charts')
    def charts():
        """Charts and analytics page."""
        from datetime import datetime, timedelta

        portfolio, _ = get_active_portfolio()

        if not app.portfolios:
            return redirect(url_for('upload'))

        # Get monthly data
        monthly_pnl = portfolio.get_monthly_realized_pnl()
        monthly_premium = portfolio.get_monthly_premium_collected()
        monthly_trades = portfolio.get_monthly_trades_count()

        # Prepare chart data
        months = sorted(set(list(monthly_pnl.keys()) + list(monthly_premium.keys())))

        # Filter trades to last 30 days
        thirty_days_ago = datetime.now() - timedelta(days=30)
        recent_trades = [
            t for t in portfolio.option_trades
            if t.trade_date >= thirty_days_ago
        ]
        # Sort by date descending (most recent first)
        recent_trades.sort(key=lambda t: t.trade_date, reverse=True)

        # Calculate unrealized P&L by underlying
        unrealized_by_underlying = {}
        for opt in portfolio.options:
            underlying = opt.underlying
            if underlying not in unrealized_by_underlying:
                unrealized_by_underlying[underlying] = {'pnl': 0, 'count': 0}
            unrealized_by_underlying[underlying]['pnl'] += opt.unrealized_pnl
            unrealized_by_underlying[underlying]['count'] += 1

        # Sort by absolute P&L value (largest impact first)
        sorted_unrealized = sorted(
            unrealized_by_underlying.items(),
            key=lambda x: abs(x[1]['pnl']),
            reverse=True
        )

        # Prepare unrealized data for chart and table
        unrealized_list = [
            {'underlying': k, 'pnl': v['pnl'], 'count': v['count']}
            for k, v in sorted_unrealized
        ]

        chart_data = {
            'months': months,
            'realized_pnl': [monthly_pnl.get(m, 0) for m in months],
            'premium_collected': [monthly_premium.get(m, 0) for m in months],
            'trades_count': [monthly_trades.get(m, 0) for m in months],
            'total_realized_pnl': portfolio.total_realized_pnl if portfolio else 0,
            'total_premium_collected': portfolio.total_premium_collected if portfolio else 0,
            'total_trades': len(portfolio.option_trades) if portfolio else 0,
            'options_unrealized_pnl': portfolio.options_unrealized_pnl if portfolio else 0,
            'unrealized_by_underlying': unrealized_list,
            'unrealized_labels': [item['underlying'] for item in unrealized_list[:10]],  # Top 10 for chart
            'unrealized_values': [item['pnl'] for item in unrealized_list[:10]],
        }

        return render_template('charts.html',
                             portfolio=portfolio,
                             chart_data=chart_data,
                             recent_trades=recent_trades,
                             portfolios=app.portfolios,
                             portfolio_filter=session.get('portfolio_filter', 'all'))

    @app.route('/api/portfolio')
    def api_portfolio():
        """API endpoint for portfolio data."""
        portfolio, _ = get_active_portfolio()
        if portfolio is None:
            return jsonify({'error': 'No portfolio loaded'}), 404
        return jsonify(portfolio.to_dict())

    @app.route('/api/refresh-prices', methods=['POST'])
    def api_refresh_prices():
        """Refresh underlying prices for all portfolios."""
        if not app.portfolios:
            return jsonify({'error': 'No portfolio loaded'}), 404

        for portfolio in app.portfolios.values():
            update_portfolio_prices(portfolio)
            calculate_portfolio_greeks(portfolio)

        return jsonify({'status': 'success', 'message': 'Prices refreshed'})
    
    return app

