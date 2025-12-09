"""Flask web application for Options Analyzer."""
import os
from flask import Flask, render_template, request, jsonify, redirect, url_for, flash
from werkzeug.utils import secure_filename

from src.data.parser import IBActivityStatementParser
from src.data.price_fetcher import update_portfolio_prices
from src.data.storage import DataStorage
from src.analysis.greeks import calculate_portfolio_greeks
from src.analysis.margin import calculate_portfolio_margin
from src.analysis.risk import analyze_portfolio_risk


def create_app(config=None):
    """Create and configure the Flask application."""
    app = Flask(__name__, 
                template_folder='../../templates',
                static_folder='../../static')
    
    app.secret_key = os.environ.get('SECRET_KEY', 'dev-secret-key-change-in-production')
    app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB max file size
    
    # Initialize storage
    storage = DataStorage()
    
    # Store in app context
    app.storage = storage
    app.portfolio = None
    app.risk_analysis = None
    
    @app.route('/')
    def index():
        """Dashboard page."""
        portfolio = app.portfolio
        risk_analysis = app.risk_analysis
        
        if portfolio is None:
            return render_template('upload.html')
        
        return render_template('dashboard.html', 
                             portfolio=portfolio,
                             risk_analysis=risk_analysis)
    
    @app.route('/upload', methods=['GET', 'POST'])
    def upload():
        """Handle file upload (supports multiple files)."""
        if request.method == 'POST':
            if 'file' not in request.files:
                flash('No file selected', 'error')
                return redirect(request.url)

            files = request.files.getlist('file')
            if not files or all(f.filename == '' for f in files):
                flash('No file selected', 'error')
                return redirect(request.url)

            try:
                # Parse all files and merge into one portfolio
                parser = IBActivityStatementParser()
                merged_portfolio = None
                files_processed = 0

                for file in files:
                    if file.filename == '':
                        continue

                    content = file.read().decode('utf-8-sig')
                    portfolio = parser.parse_content(content)

                    if merged_portfolio is None:
                        merged_portfolio = portfolio
                    else:
                        merged_portfolio.merge(portfolio)

                    files_processed += 1

                if merged_portfolio is None:
                    flash('No valid files found', 'error')
                    return redirect(request.url)

                # Fetch missing prices
                update_portfolio_prices(merged_portfolio)

                # Calculate Greeks
                calculate_portfolio_greeks(merged_portfolio)

                # Calculate Margin
                calculate_portfolio_margin(merged_portfolio, margin_type="reg_t")

                # Analyze Risk
                config = storage.get_config()
                risk_analysis = analyze_portfolio_risk(merged_portfolio, config)

                # Store in app context
                app.portfolio = merged_portfolio
                app.risk_analysis = risk_analysis

                # Save to file
                storage.save_portfolio(merged_portfolio)

                if files_processed > 1:
                    flash(f'Merged {files_processed} files: {merged_portfolio.total_option_positions} options, {len(merged_portfolio.trades)} trades', 'success')
                else:
                    flash(f'Successfully loaded {merged_portfolio.total_option_positions} options', 'success')
                return redirect(url_for('index'))

            except Exception as e:
                flash(f'Error parsing file: {str(e)}', 'error')
                return redirect(request.url)

        return render_template('upload.html')
    
    @app.route('/positions')
    def positions():
        """Positions table page."""
        portfolio = app.portfolio
        if portfolio is None:
            return redirect(url_for('upload'))

        # Get filter parameters
        filter_type = request.args.get('type', 'all')  # all, calls, puts
        filter_risk = request.args.get('risk', 'all')  # all, high, flagged
        sort_by = request.args.get('sort', 'underlying')
        sort_dir = request.args.get('dir', 'asc')  # asc or desc

        options = portfolio.options

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
                             sort_dir=sort_dir)
    
    @app.route('/recommendations')
    def recommendations():
        """Recommendations page."""
        portfolio = app.portfolio
        risk_analysis = app.risk_analysis

        if portfolio is None:
            return redirect(url_for('upload'))

        return render_template('recommendations.html',
                             portfolio=portfolio,
                             risk_analysis=risk_analysis)

    @app.route('/charts')
    def charts():
        """Charts and analytics page."""
        portfolio = app.portfolio

        if portfolio is None:
            return redirect(url_for('upload'))

        # Get monthly data
        monthly_pnl = portfolio.get_monthly_realized_pnl()
        monthly_premium = portfolio.get_monthly_premium_collected()
        monthly_trades = portfolio.get_monthly_trades_count()

        # Prepare chart data
        months = sorted(set(list(monthly_pnl.keys()) + list(monthly_premium.keys())))

        chart_data = {
            'months': months,
            'realized_pnl': [monthly_pnl.get(m, 0) for m in months],
            'premium_collected': [monthly_premium.get(m, 0) for m in months],
            'trades_count': [monthly_trades.get(m, 0) for m in months],
            'total_realized_pnl': portfolio.total_realized_pnl,
            'total_premium_collected': portfolio.total_premium_collected,
            'total_trades': len(portfolio.option_trades),
        }

        return render_template('charts.html',
                             portfolio=portfolio,
                             chart_data=chart_data)

    @app.route('/api/portfolio')
    def api_portfolio():
        """API endpoint for portfolio data."""
        portfolio = app.portfolio
        if portfolio is None:
            return jsonify({'error': 'No portfolio loaded'}), 404
        return jsonify(portfolio.to_dict())
    
    @app.route('/api/refresh-prices', methods=['POST'])
    def api_refresh_prices():
        """Refresh underlying prices."""
        portfolio = app.portfolio
        if portfolio is None:
            return jsonify({'error': 'No portfolio loaded'}), 404
        
        update_portfolio_prices(portfolio)
        calculate_portfolio_greeks(portfolio)
        
        return jsonify({'status': 'success', 'message': 'Prices refreshed'})
    
    return app

