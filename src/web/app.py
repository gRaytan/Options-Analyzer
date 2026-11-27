"""Flask web application for Options Analyzer."""
import os
from datetime import date, datetime
import secrets
from flask import Flask, render_template, request, jsonify, redirect, url_for, flash, session
from werkzeug.utils import secure_filename

from src.data.parser import IBActivityStatementParser
from src.data.price_fetcher import update_portfolio_prices
from src.data.storage import DataStorage
from src.analysis.greeks import calculate_portfolio_greeks
from src.analysis.margin import calculate_portfolio_margin
from src.analysis.risk import HIGH_RISK_FLAGS, analyze_portfolio_risk
from src.models.portfolio import Portfolio
from src.trading.config import load_trading_config
from src.trading.domain import HaltScope
from src.trading.journal import DEFAULT_JOURNAL_PATH, Journal
from src.trading.approvals import ApprovalError, ApprovalQueue
from src.trading.kill_switch import DEFAULT_HALT_FILE, HaltManager
from src.trading.status import load_bot_status


def create_app(config=None):
    """Create and configure the Flask application."""
    app = Flask(__name__,
                template_folder='../../templates',
                static_folder='../../static')

    # A random key when none is configured: never a guessable default.
    app.secret_key = os.environ.get('SECRET_KEY') or secrets.token_hex(32)
    app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB max file size

    # Initialize storage
    storage = DataStorage()

    # Store in app context - now supports multiple portfolios
    app.storage = storage
    app.jinja_env.globals['HIGH_RISK_FLAGS'] = HIGH_RISK_FLAGS
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
            combined.dividends = []
            combined.bond_interest = []
            combined.cash_transactions = []
            combined.net_asset_value = 0
            combined.cash_balance = 0
            combined.period_start = None
            combined.period_end = None
            combined.statement_period = ""
            account_names = []

            for pid, p in app.portfolios.items():
                # Merge period ranges - expand to cover all portfolios
                if p.period_start:
                    if combined.period_start is None or p.period_start < combined.period_start:
                        combined.period_start = p.period_start
                if p.period_end:
                    if combined.period_end is None or p.period_end > combined.period_end:
                        combined.period_end = p.period_end
                if p.statement_period:
                    if combined.statement_period:
                        combined.statement_period += " + " + p.statement_period
                    else:
                        combined.statement_period = p.statement_period
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

                # Combine all dividends (deduplicate by key)
                existing_dividends = {
                    (d.symbol, d.payment_date, d.amount): d
                    for d in combined.dividends
                }
                for div in p.dividends:
                    key = (div.symbol, div.payment_date, div.amount)
                    if key not in existing_dividends:
                        combined.dividends.append(div)
                        existing_dividends[key] = div

                # Combine all bond interest (deduplicate by key)
                existing_bond_interest = {
                    (b.symbol, b.payment_date, b.amount): b
                    for b in combined.bond_interest
                }
                for interest in getattr(p, 'bond_interest', []):
                    key = (interest.symbol, interest.payment_date, interest.amount)
                    if key not in existing_bond_interest:
                        combined.bond_interest.append(interest)
                        existing_bond_interest[key] = interest

                # Combine all cash transactions (deposits/withdrawals) - deduplicate by key
                existing_cash_transactions = {
                    (t.transaction_date, t.amount, t.description): t
                    for t in combined.cash_transactions
                }
                for transaction in getattr(p, 'cash_transactions', []):
                    key = (transaction.transaction_date, transaction.amount, transaction.description)
                    if key not in existing_cash_transactions:
                        combined.cash_transactions.append(transaction)
                        existing_cash_transactions[key] = transaction

                # Sum up NAV and cash
                if p.net_asset_value:
                    combined.net_asset_value += p.net_asset_value
                if p.cash_balance:
                    combined.cash_balance += p.cash_balance

                # Collect account names
                if p.account_name:
                    account_names.append(p.account_name)

            combined.account_name = " + ".join(account_names) if account_names else "Combined"

            # Margin figures (Cash Report): the latest statement of each account,
            # summed across accounts. Several statements of one account are
            # different dates of the same money, so they aren't added up.
            latest = {}
            for pid, p in app.portfolios.items():
                acct = p.account_id or pid
                cur = latest.get(acct)
                if cur is None or (p.period_end or date.min) >= (cur.period_end or date.min):
                    latest[acct] = p
            for field_name in ("net_liquidation_value", "excess_liquidity", "maintenance_margin",
                               "initial_margin", "available_funds", "buying_power", "sma",
                               "gross_position_value"):
                setattr(combined, field_name,
                        sum(getattr(p, field_name, 0.0) or 0.0 for p in latest.values()))
            if len(latest) == 1:
                combined.account_id = next(iter(latest))

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

        # Calculate YTD revenue (realized P&L + dividends + bond interest + unrealized P&L - deposits)
        ytd_realized_pnl = portfolio.total_realized_pnl
        ytd_dividends = portfolio.total_dividends
        ytd_bond_interest = portfolio.total_bond_interest
        ytd_unrealized_pnl = portfolio.total_unrealized_pnl
        net_deposits = portfolio.net_deposits  # Deposits minus withdrawals
        ytd_revenue = ytd_realized_pnl + ytd_dividends + ytd_bond_interest + ytd_unrealized_pnl - net_deposits

        # Debug: print YTD revenue breakdown
        print(f"YTD Revenue Breakdown:")
        print(f"  Realized P&L: ${ytd_realized_pnl:,.2f}")
        print(f"  Dividends: ${ytd_dividends:,.2f}")
        print(f"  Bond Interest: ${ytd_bond_interest:,.2f}")
        print(f"  Unrealized P&L: ${ytd_unrealized_pnl:,.2f}")
        print(f"    - Options Unrealized: ${portfolio.options_unrealized_pnl:,.2f}")
        print(f"    - Stocks Unrealized: ${portfolio.stocks_unrealized_pnl:,.2f}")
        # Check for stocks with 0 price
        zero_price_stocks = [s for s in portfolio.stocks if s.current_price == 0]
        if zero_price_stocks:
            print(f"    WARNING: {len(zero_price_stocks)} stocks have price=0: {[s.symbol for s in zero_price_stocks[:5]]}")
        # Check for options with 0 price
        zero_price_options = [o for o in portfolio.options if o.current_price == 0]
        if zero_price_options:
            print(f"    WARNING: {len(zero_price_options)} options have price=0: {[o.symbol for o in zero_price_options[:5]]}")
        print(f"  Net Deposits: ${net_deposits:,.2f}")
        print(f"  TOTAL YTD Revenue: ${ytd_revenue:,.2f}")
        print(f"  Number of stocks: {len(portfolio.stocks)}, options: {len(portfolio.options)}")

        # Calculate YTD return percentage (based on NAV minus net deposits)
        # This gives the actual return on the original investment
        adjusted_nav = portfolio.net_asset_value - net_deposits
        ytd_return_pct = (ytd_revenue / adjusted_nav * 100) if adjusted_nav > 0 else 0

        return render_template('dashboard.html',
                             portfolio=portfolio,
                             risk_analysis=risk_analysis,
                             ytd_revenue=ytd_revenue,
                             ytd_return_pct=ytd_return_pct,
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

                    # Store in app context immediately (don't wait for prices)
                    app.portfolios[portfolio_id] = portfolio
                    files_processed += 1

                if files_processed == 0:
                    flash('No valid files found', 'error')
                    return redirect(request.url)

                # Fetch prices and calculate Greeks/Risk in background thread
                import threading
                def background_data_fetch():
                    """Fetch prices and dividend data in background."""
                    from src.analysis.income_projection import get_forward_dividend_rate
                    import time

                    # First, fetch prices for all portfolios
                    for pid, p in app.portfolios.items():
                        try:
                            update_portfolio_prices(p)
                            calculate_portfolio_greeks(p)
                            calculate_portfolio_margin(p, margin_type="reg_t")

                            # Analyze Risk
                            config = storage.get_config()
                            risk_analysis = analyze_portfolio_risk(p, config)
                            app.risk_analyses[pid] = risk_analysis
                        except Exception as e:
                            print(f"Background fetch error for {pid}: {e}")

                    # Then pre-fetch dividend data
                    all_symbols = set()
                    for p in app.portfolios.values():
                        for stock in p.stocks:
                            all_symbols.add(stock.symbol)

                    for symbol in all_symbols:
                        try:
                            get_forward_dividend_rate(symbol)
                            time.sleep(0.5)  # Rate limit
                        except Exception:
                            pass

                thread = threading.Thread(target=background_data_fetch, daemon=True)
                thread.start()

                flash(f'Loaded {files_processed} portfolio(s). Prices updating in background...', 'success')
                return redirect(url_for('index'))

            except Exception as e:
                flash(f'Error parsing file: {str(e)}', 'error')
                return redirect(request.url)

        return render_template('upload.html',
                             portfolios=app.portfolios,
                             portfolio_filter=session.get('portfolio_filter', 'all'))

    @app.route('/refresh_prices')
    def refresh_prices():
        """Refresh stock prices for all portfolios."""
        from src.data.price_fetcher import PriceFetcher

        if not app.portfolios:
            flash('No portfolios loaded', 'error')
            return redirect(url_for('upload'))

        try:
            fetcher = PriceFetcher()
            fetcher.clear_cache()  # Clear cache to force fresh prices

            total_updated = 0
            for portfolio_id, portfolio in app.portfolios.items():
                # Fetch fresh prices (don't reset to 0 - keep statement prices as fallback)
                update_portfolio_prices(portfolio, fetcher)

                # Also update stock prices (only if we get valid prices)
                stock_symbols = list(set(s.symbol for s in portfolio.stocks if s.symbol))
                if stock_symbols:
                    prices = fetcher.get_prices(stock_symbols)
                    for stock in portfolio.stocks:
                        if stock.symbol in prices and prices[stock.symbol]:
                            stock.current_price = prices[stock.symbol]

                # Recalculate Greeks with new prices
                calculate_portfolio_greeks(portfolio)

                total_updated += len(portfolio.options) + len(portfolio.stocks)

            flash(f'Prices refreshed for {total_updated} positions', 'success')
        except Exception as e:
            flash(f'Error refreshing prices: {str(e)}', 'error')

        # Redirect back to the referring page or index
        return redirect(request.referrer or url_for('index'))

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
            options = [o for o in options if HIGH_RISK_FLAGS.intersection(o.flags)]
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

    @app.route('/stocks')
    def stocks():
        """Stocks holdings table page."""
        portfolio, _ = get_active_portfolio()
        if not app.portfolios:
            return redirect(url_for('upload'))

        # Get sort parameters
        sort_by = request.args.get('sort', 'symbol')
        sort_dir = request.args.get('dir', 'asc')

        stock_list = portfolio.stocks if portfolio else []

        # Aggregate stocks by symbol
        aggregated = {}
        for stock in stock_list:
            symbol = stock.symbol
            if symbol in aggregated:
                agg = aggregated[symbol]
                # Combine quantities and cost basis, recalculate weighted avg cost
                new_qty = agg['quantity'] + stock.quantity
                new_cost_basis = agg['cost_basis'] + stock.cost_basis
                new_market_value = agg['market_value'] + stock.market_value
                agg['quantity'] = new_qty
                agg['cost_basis'] = new_cost_basis
                agg['market_value'] = new_market_value
                agg['cost_price'] = new_cost_basis / new_qty if new_qty else 0
                agg['current_price'] = stock.current_price  # Same for all
                agg['unrealized_pnl'] = new_market_value - new_cost_basis
                agg['unrealized_pnl_pct'] = ((new_market_value - new_cost_basis) / new_cost_basis * 100) if new_cost_basis else 0
            else:
                aggregated[symbol] = {
                    'symbol': symbol,
                    'quantity': stock.quantity,
                    'cost_price': stock.cost_price,
                    'current_price': stock.current_price,
                    'cost_basis': stock.cost_basis,
                    'market_value': stock.market_value,
                    'unrealized_pnl': stock.unrealized_pnl,
                    'unrealized_pnl_pct': stock.unrealized_pnl_pct,
                }

        # Convert to list of dict-like objects for template
        class AggregatedStock:
            def __init__(self, data):
                for k, v in data.items():
                    setattr(self, k, v)

        stock_list = [AggregatedStock(data) for data in aggregated.values()]

        # Sort configuration: column -> (key_func, default_reverse)
        sort_config = {
            'symbol': (lambda x: x.symbol.lower(), False),
            'quantity': (lambda x: x.quantity, False),
            'cost_price': (lambda x: x.cost_price, False),
            'current_price': (lambda x: x.current_price, False),
            'market_value': (lambda x: x.market_value, True),
            'cost_basis': (lambda x: x.cost_basis, True),
            'pnl': (lambda x: x.unrealized_pnl, True),
            'pnl_pct': (lambda x: x.unrealized_pnl_pct, True),
        }

        if sort_by in sort_config:
            key_func, _ = sort_config[sort_by]
            reverse = (sort_dir == 'desc')
            stock_list = sorted(stock_list, key=key_func, reverse=reverse)

        # Calculate totals
        total_market_value = sum(s.market_value for s in stock_list)
        total_cost_basis = sum(s.cost_basis for s in stock_list)
        total_unrealized_pnl = sum(s.unrealized_pnl for s in stock_list)

        return render_template('stocks.html',
                             stocks=stock_list,
                             portfolio=portfolio,
                             sort_by=sort_by,
                             sort_dir=sort_dir,
                             total_market_value=total_market_value,
                             total_cost_basis=total_cost_basis,
                             total_unrealized_pnl=total_unrealized_pnl,
                             portfolios=app.portfolios,
                             portfolio_filter=session.get('portfolio_filter', 'all'))

    @app.route('/balance')
    def balance():
        """Balance information page - always shows per-portfolio data."""
        portfolio, risk_analysis = get_active_portfolio()
        if not app.portfolios:
            return redirect(url_for('upload'))

        # Always build per-portfolio margin data with recommendations
        portfolio_margins = []
        for name, p in app.portfolios.items():
            utilization = p.margin_utilization

            # Build recommendations to increase margin for this portfolio
            recommendations = []

            # 1. Check for negative cash (margin loan) - paying this down increases margin
            if p.cash_balance < 0:
                recommendations.append({
                    'severity': 'high',
                    'title': 'Pay Down Margin Loan',
                    'message': f'Deposit ${abs(p.cash_balance):,.0f} to eliminate margin loan and stop interest charges. This directly increases available margin.',
                    'icon': 'cash-stack'
                })

            # 2. Close profitable short options to free up margin
            profitable_shorts = [o for o in p.short_options if o.unrealized_pnl > 0]
            if profitable_shorts:
                total_profit = sum(o.unrealized_pnl for o in profitable_shorts)
                recommendations.append({
                    'severity': 'medium',
                    'title': 'Close Profitable Short Options',
                    'message': f'You have {len(profitable_shorts)} profitable short options with ${total_profit:,.0f} in unrealized gains. Closing these frees up margin.',
                    'icon': 'graph-up-arrow'
                })

            # 3. Roll short options to reduce notional exposure
            high_strike_puts = [o for o in p.short_options if o.is_put and o.strike > 100]
            if high_strike_puts:
                recommendations.append({
                    'severity': 'low',
                    'title': 'Roll Down High-Strike Puts',
                    'message': f'Consider rolling {len(high_strike_puts)} high-strike puts to lower strikes to reduce margin requirements.',
                    'icon': 'arrow-down-circle'
                })

            # 4. Convert naked options to spreads
            naked_shorts = len(p.short_options)
            if naked_shorts > 5:
                recommendations.append({
                    'severity': 'low',
                    'title': 'Convert to Spreads',
                    'message': f'You have {naked_shorts} short options. Converting naked positions to spreads (buying protective options) significantly reduces margin.',
                    'icon': 'layers'
                })

            # 5. Sell covered calls on stock positions
            if p.stocks and len(p.stocks) > 0:
                stock_value = p.stocks_market_value
                recommendations.append({
                    'severity': 'low',
                    'title': 'Sell Covered Calls',
                    'message': f'You have ${stock_value:,.0f} in stocks. Selling covered calls generates premium and doesn\'t require additional margin.',
                    'icon': 'telephone-outbound'
                })

            # 6. Deposit additional funds
            recommendations.append({
                'severity': 'low',
                'title': 'Deposit Funds',
                'message': 'Depositing additional cash directly increases your available margin and buying power.',
                'icon': 'plus-circle'
            })

            portfolio_margins.append({
                'name': name,
                'net_liquidation_value': p.net_asset_value,
                'cash_balance': p.cash_balance,
                'margin_utilization': utilization,
                'short_options_count': len(p.short_options),
                'long_options_count': len([o for o in p.options if o.quantity > 0]),
                'stock_positions_count': len(p.stocks),
                'options_market_value': p.options_market_value,
                'stocks_market_value': p.stocks_market_value,
                'recommendations': recommendations,
            })

        return render_template('balance.html',
                             portfolio=portfolio,
                             risk_analysis=risk_analysis,
                             portfolio_margins=portfolio_margins,
                             portfolios=app.portfolios,
                             portfolio_filter=session.get('portfolio_filter', 'all'))

    def journal_path():
        return os.environ.get('TRADING_JOURNAL', str(DEFAULT_JOURNAL_PATH))

    def halt_file():
        return os.environ.get('TRADING_HALT_FILE', str(DEFAULT_HALT_FILE))

    def bot_status():
        config = load_trading_config()
        return config, load_bot_status(
            journal_path(),
            os.environ.get('TRADING_HEARTBEAT', 'data/trading/heartbeat.json'),
            config.margin_floor,
            halt_file=halt_file(),
        )

    def csrf_token():
        """Per-session token for the bot's POST forms (validation finding 7)."""
        if '_csrf' not in session:
            session['_csrf'] = secrets.token_urlsafe(32)
        return session['_csrf']

    app.jinja_env.globals['csrf_token'] = csrf_token

    def csrf_ok():
        sent = request.form.get('csrf_token', '')
        expected = session.get('_csrf', '')
        return bool(expected) and secrets.compare_digest(sent, expected)

    @app.route('/bot/halt', methods=['POST'])
    def bot_halt():
        """Kill switch from the UI (spec §6.4)."""
        if not csrf_ok():
            # Halting is the safe direction, but a forged request still isn't trusted.
            flash("Form expired; please try again.", 'danger')
            return redirect(url_for('bot'))
        scope = HaltScope.ALL if request.form.get('scope') == 'all' else HaltScope.OPENING
        reason = (request.form.get('reason') or 'manual halt from web UI').strip()[:200]
        with Journal(journal_path()) as journal:
            HaltManager(journal, halt_file()).halt(reason, scope, by='web-ui')
        flash(f"Bot halted ({scope.value}).", 'warning')
        return redirect(url_for('bot'))

    @app.route('/bot/resume', methods=['POST'])
    def bot_resume():
        if not csrf_ok():
            flash("Form expired; please try again.", 'danger')
            return redirect(url_for('bot'))
        if request.form.get('confirm') != 'yes':
            flash("Tick the confirmation box to resume.", 'danger')
            return redirect(url_for('bot'))
        with Journal(journal_path()) as journal:
            HaltManager(journal, halt_file()).resume(by='web-ui', note=request.form.get('note', ''))
        flash("Halt cleared.", 'success')
        return redirect(url_for('bot'))

    @app.route('/bot')
    def bot():
        """Trading bot status (read-only; data written by the engine)."""
        config, status = bot_status()
        hedge_plan = None
        if os.path.exists(journal_path()):
            with Journal(journal_path()) as journal:
                rec = journal.latest("job_runs", key="hedge")
                hedge_plan = rec.data if rec else None
        return render_template('bot_status.html',
                             status=status,
                             hedge=hedge_plan,
                             config=config,
                             floor=config.margin_floor,
                             portfolios=app.portfolios,
                             portfolio_filter=session.get('portfolio_filter', 'all'))

    @app.route('/bot/approvals')
    def bot_approvals():
        """Stage 3: approve or reject each opening trade (spec §11)."""
        pending, recent = [], []
        if os.path.exists(journal_path()):
            with Journal(journal_path()) as journal:
                queue = ApprovalQueue(journal)
                pending, recent = queue.pending(), queue.recent()
        return render_template('bot_approvals.html',
                               pending=pending, recent=recent,
                               portfolios=app.portfolios,
                               portfolio_filter=session.get('portfolio_filter', 'all'))

    @app.route('/bot/approvals/<intent_id>', methods=['POST'])
    def bot_approval_decision(intent_id):
        if not csrf_ok():
            flash("Form expired; please try again.", 'danger')
            return redirect(url_for('bot_approvals'))
        decision = request.form.get('decision')
        with Journal(journal_path()) as journal:
            queue = ApprovalQueue(journal)
            try:
                if decision == 'approve':
                    queue.approve(intent_id, by='web-ui', note=request.form.get('note', ''))
                    flash("Approved. The bot will send the order.", 'success')
                elif decision == 'reject':
                    queue.reject(intent_id, by='web-ui', note=request.form.get('note', ''))
                    flash("Rejected. Nothing will be sent.", 'warning')
            except ApprovalError as exc:
                flash(str(exc), 'danger')
        nxt = request.form.get("next", "")
        if nxt.startswith("/bot/") and "//" not in nxt:
            return redirect(nxt)
        return redirect(url_for("bot_approvals"))

    @app.route("/bot/rebuild", methods=["GET", "POST"])
    def bot_rebuild():
        """Rebuild after a margin call: targets -> a staged plan, approve each item."""
        from datetime import datetime as _dt, timezone as _tz
        from src.trading import rebuild as rb
        from src.trading.domain import StockContract

        if request.method == "POST":
            if not csrf_ok():
                flash("Form expired; please try again.", "danger")
                return redirect(url_for("bot_rebuild"))
            try:
                targets = rb.parse_targets(request.form.get("targets", ""))
            except ValueError as exc:
                flash(f"Targets: {exc}", "danger")
                return redirect(url_for("bot_rebuild"))
            now = _dt.now(_tz.utc)
            with Journal(journal_path()) as journal:
                journal.append(
                    "rebuild_requests",
                    {
                        "request_id": rb.new_plan_id("web", now),
                        "targets": targets,
                        "options": request.form.get("options") == "on",
                        "by": "web-ui",
                        "requested_at": now.isoformat(),
                    },
                )
            flash(
                "Plan requested. The engine builds it on its next pass in market "
                "hours (within 5 minutes) and then asks you to approve each item.",
                "success",
            )
            return redirect(url_for("bot_rebuild"))

        account = positions = req = plan = None
        items = []
        if os.path.exists(journal_path()):
            with Journal(journal_path()) as journal:
                rec = journal.latest("account_snapshot")
                account = rec.data if rec else None
                rec = journal.latest("positions_snapshot")
                positions = list(rec.data) if rec else []
                rec = journal.latest("rebuild_requests")
                req = rec.data if rec else None
                rec = journal.latest("rebuild_plans")
                plan = rec.data if rec else None
                if plan is not None:
                    queue = ApprovalQueue(journal)
                    for it in plan.get("intents", []):
                        filled = sum(
                            f.data.quantity
                            for f in journal.read("fills", key=it.intent_id)
                        )
                        status = queue.status(it.intent_id).value
                        items.append({"intent": it, "status": status, "filled": filled})
        stocks = [
            p
            for p in (positions or [])
            if isinstance(p.contract, StockContract) and p.quantity > 0
        ]
        stock_value = sum(p.quantity * (p.market_price or p.avg_cost) for p in stocks)
        last_targets = (req or {}).get("targets") or {}
        if last_targets:
            default_targets = ", ".join(
                f"{k}={v * 100:g}%" for k, v in last_targets.items()
            )
        elif stocks and stock_value > 0:
            default_targets = ", ".join(
                f"{p.contract.symbol}={p.quantity * (p.market_price or p.avg_cost) / stock_value * 100:.0f}%"
                for p in stocks
            )
        else:
            default_targets = "SPY=50%, QQQM=50%"
        waiting = bool(
            req and (plan is None or plan.get("request_id") != req.get("request_id"))
        )
        _, live = bot_status()
        crash = (live.budget or {}).get("crash")
        return render_template(
            "bot_rebuild.html",
            account=account,
            crash=crash,
            stocks=stocks,
            stock_value=stock_value,
            req=req,
            plan=plan,
            items=items,
            waiting=waiting,
            default_targets=default_targets,
            config=load_trading_config(),
            portfolios=app.portfolios,
            portfolio_filter=session.get("portfolio_filter", "all"),
        )

    @app.route('/bot/check')
    def bot_check():
        """Crash test and rebuild preview from the uploaded IB statement (no IB needed)."""
        from datetime import date as _date
        from src.trading import rebuild as rb
        from src.trading import statement as st

        portfolio, _ = get_active_portfolio()
        if portfolio is None:
            return render_template('bot_check.html', book=None,
                                   portfolios=app.portfolios,
                                   portfolio_filter=session.get('portfolio_filter', 'all'))
        config = load_trading_config()
        book = st.from_portfolio(portfolio)
        rows = st.crash_table(book, config)
        worst = st.by_position(book, config, config.risk.crash_move)
        from src.trading import derisk as dr
        from src.trading.run import external_shares

        derisk_plan = dr.plan(book.positions, book.quotes, book.account, config,
                              "statement", external_shares=external_shares())
        targets_text = request.args.get('targets', '').strip()
        preview = None
        if targets_text:
            try:
                targets = rb.parse_targets(targets_text)
                prices = book.prices()
                steps, fit, notes = rb.plan_shares(
                    book.positions, book.quotes, book.account, prices, targets,
                    config, request.args.get('regime', 'elevated'), book.as_of or _date.today())
                preview = {"steps": steps, "fit": fit, "notes": notes, "targets": targets}
            except ValueError as exc:
                flash(f"Targets: {exc}", 'danger')
        return render_template('bot_check.html', book=book, rows=rows, worst=worst,
                               derisk=derisk_plan,
                               preview=preview, targets_text=targets_text or "SPY=40%, QQQM=40%, BRK.B=12%, NBIS=5%",
                               config=config, portfolio=portfolio,
                               portfolios=app.portfolios,
                               portfolio_filter=session.get('portfolio_filter', 'all'))

    @app.route('/bot/derisk', methods=['GET', 'POST'])
    def bot_derisk():
        """De-risk plan from live positions: buy-backs, each approved by the owner."""
        from datetime import datetime as _dt, timezone as _tz
        from src.trading import rebuild as rb

        if request.method == 'POST':
            if not csrf_ok():
                flash("Form expired; please try again.", 'danger')
                return redirect(url_for('bot_derisk'))
            now = _dt.now(_tz.utc)
            with Journal(journal_path()) as journal:
                journal.append("derisk_requests", {
                    "request_id": rb.new_plan_id("derisk", now),
                    "penny_calls": request.form.get('penny_calls') == 'on',
                    "by": "web-ui",
                    "requested_at": now.isoformat(),
                })
            flash("De-risk plan requested. The engine builds it from live prices on its next "
                  "pass in market hours, then asks you to approve each buy-back.", 'success')
            return redirect(url_for('bot_derisk'))
        req = plan = None
        items = []
        if os.path.exists(journal_path()):
            with Journal(journal_path()) as journal:
                rec = journal.latest("derisk_requests")
                req = rec.data if rec else None
                rec = journal.latest("derisk_plans")
                plan = rec.data if rec else None
                if plan is not None:
                    queue = ApprovalQueue(journal)
                    for it in plan.get("intents", []):
                        filled = sum(f.data.quantity for f in journal.read("fills", key=it.intent_id))
                        items.append({"intent": it, "status": queue.status(it.intent_id).value,
                                      "filled": filled})
        waiting = bool(req and (plan is None or plan.get("request_id") != req.get("request_id")))
        return render_template('bot_derisk.html', req=req, plan=plan, items=items, waiting=waiting,
                               portfolios=app.portfolios,
                               portfolio_filter=session.get('portfolio_filter', 'all'))

    @app.route('/bot/external', methods=['GET', 'POST'])
    def bot_external():
        """Covered calls at another broker: recommendations, open calls, record trades."""
        from datetime import date as _date, datetime as _dt, timezone as _tz
        from src.trading import external_calls as xc
        from src.trading.run import external_holdings_data

        if request.method == 'POST':
            if not csrf_ok():
                flash("Form expired; please try again.", 'danger')
                return redirect(url_for('bot_external'))
            f = request.form
            try:
                action = f.get('action', 'sell')
                if action == 'sell':
                    expiry = _date.fromisoformat(f['expiry'])
                    strike, contracts, price = float(f['strike']), int(f['contracts']), float(f['price'])
                    sold_on = _date.fromisoformat(f.get('date') or _date.today().isoformat())
                    if contracts <= 0 or price < 0 or strike <= 0:
                        raise ValueError("contracts, strike and price must be positive")
                    rec = {"action": "sell", "symbol": f['symbol'].strip().upper(), "expiry": expiry.isoformat(),
                           "strike": strike, "contracts": contracts, "price": price, "date": sold_on.isoformat(),
                           "trade_id": xc.record_id(f['symbol'], expiry, strike, sold_on)}
                else:
                    if action not in ('buy', 'expired', 'assigned'):
                        raise ValueError("unknown action")
                    rec = {"action": action, "trade_id": f['trade_id'], "price": float(f.get('price') or 0),
                           "date": _date.today().isoformat()}
            except (KeyError, ValueError) as exc:
                flash(f"Not recorded: {exc}", 'danger')
                return redirect(url_for('bot_external'))
            rec["recorded_at"] = _dt.now(_tz.utc).isoformat()
            with Journal(journal_path()) as journal:
                journal.append("external_trades", rec, key=rec["trade_id"])
            flash("Recorded.", 'success')
            return redirect(url_for('bot_external'))

        holdings = xc.holdings_from(external_holdings_data())
        records, recs = [], None
        if os.path.exists(journal_path()):
            with Journal(journal_path()) as journal:
                records = [r.data for r in journal.read("external_trades")]
                rec = journal.latest("job_runs", key="external_calls")
                recs = rec.data if rec else None
        today = _date.today()
        open_ = xc.open_calls(records, today)
        sold = [r for r in records if r.get("action") == "sell"]
        return render_template('bot_external.html', holdings=holdings, open_calls=open_,
                               sold=sold, recs=recs, today=today,
                               portfolios=app.portfolios,
                               portfolio_filter=session.get('portfolio_filter', 'all'))

    @app.route('/bot/opportunities')
    def bot_opportunities():
        """Latest scanner results (T30) with timing (T31)."""
        scan, buys = None, None
        if os.path.exists(journal_path()):
            with Journal(journal_path()) as journal:
                rec = journal.latest("scan_results")
                if rec is not None:
                    scan = {"ts": rec.ts, **rec.data}
                b = journal.latest("job_runs", key="buy_ideas")
                if b is not None:
                    buys = b.data
        return render_template('bot_opportunities.html', scan=scan, buys=buys,
                               portfolios=app.portfolios,
                               portfolio_filter=session.get('portfolio_filter', 'all'))

    @app.route('/bot/premium')
    def bot_premium():
        """Premium timing: today's premium vs its history, and graded sell/wait calls."""
        from datetime import date as _date
        from src.trading import premium

        records, spike_rows = [], []
        if os.path.exists(journal_path()):
            with Journal(journal_path()) as journal:
                records = [r.data for r in journal.read("scan_results")]
                spike_rows = [r.data for r in journal.read("spike_events")][-20:][::-1]
        r = premium.report(records, _date.today().isoformat())
        return render_template('bot_premium.html', r=r, spikes=spike_rows,
                               portfolios=app.portfolios,
                               portfolio_filter=session.get('portfolio_filter', 'all'))

    @app.route('/bot/metrics')
    def bot_metrics():
        """Bot performance from the journal (T40): win rate, fees, slippage, P&L."""
        from datetime import date as _date
        from src.trading import metrics as bot_metrics_mod

        m = None
        external = []
        if os.path.exists(journal_path()):
            with Journal(journal_path()) as journal:
                m = bot_metrics_mod.compute(journal, today=_date.today())
                external = [r.data for r in journal.read("external_trades")]
        goal = load_trading_config().income_goal_monthly
        income = bot_metrics_mod.income_by_month(m.months if m else {}, external)
        this_month = _date.today().strftime("%Y-%m")
        income_view = {"goal": goal, "months": list(income), "amounts": list(income.values()),
                       "this_month": income.get(this_month, 0.0), "month": this_month}
        chart = None
        if m is not None:
            chart = {
                'months': list(m.months),
                'premium': [v['premium'] for v in m.months.values()],
                'pnl': [v['pnl'] for v in m.months.values()],
                'fees': [v['fees'] for v in m.months.values()],
                'nlv_days': [d.isoformat() for d, _ in m.nlv],
                'nlv': [v for _, v in m.nlv],
            }
        years = sorted({t.closed.year for t in m.closed}) if m else []
        return render_template('bot_metrics.html', m=m, chart=chart, years=years,
                               income=income_view,
                               portfolios=app.portfolios,
                               portfolio_filter=session.get('portfolio_filter', 'all'))

    @app.route('/bot/metrics/realized.csv')
    def bot_realized_csv():
        """Closed trades for one year (USD, fees included), for the tax return."""
        from datetime import date as _date
        from flask import Response
        from src.trading import metrics as bot_metrics_mod

        year = request.args.get('year', type=int)
        text = bot_metrics_mod.realized_csv([], year)
        if os.path.exists(journal_path()):
            with Journal(journal_path()) as journal:
                m = bot_metrics_mod.compute(journal, today=_date.today())
            text = bot_metrics_mod.realized_csv(m.trades, year)
        name = f"realized_{year or 'all'}.csv"
        return Response(text, mimetype='text/csv',
                        headers={'Content-Disposition': f'attachment; filename={name}'})

    @app.route('/api/bot-status')
    def api_bot_status():
        _, status = bot_status()
        return jsonify(status.to_json())

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

    @app.route('/dividends')
    def dividends():
        """Dividends page with charts and projections."""
        from datetime import datetime, timedelta
        from collections import defaultdict

        portfolio, _ = get_active_portfolio()

        if not app.portfolios:
            return redirect(url_for('upload'))

        # Get monthly dividend data
        monthly_dividends = portfolio.get_monthly_dividends()
        months = sorted(monthly_dividends.keys())

        # Get dividends by symbol
        dividends_by_symbol = portfolio.get_dividends_by_symbol()

        # Build stock info with dividend yield
        # Only include stocks that are currently held (shares > 0)
        dividend_stocks = []
        for symbol, div_data in dividends_by_symbol.items():
            # Find ALL stock positions for this symbol (may have multiple from different portfolios)
            matching_stocks = [s for s in portfolio.stocks if s.symbol == symbol]
            shares = sum(s.quantity for s in matching_stocks) if matching_stocks else 0

            # Skip stocks that are no longer held
            if shares <= 0:
                continue

            # Use the first stock's current price (should be same for all)
            current_price = matching_stocks[0].current_price if matching_stocks else 0
            market_value = shares * current_price if shares and current_price else 0

            # Calculate annual dividend and yield
            annual_dividend = div_data['total']  # YTD dividends
            # Annualize based on months of data
            if months:
                months_of_data = len(months)
                if months_of_data > 0:
                    annual_dividend = (div_data['total'] / months_of_data) * 12

            dividend_yield = (annual_dividend / market_value * 100) if market_value > 0 else 0

            dividend_stocks.append({
                'symbol': symbol,
                'shares': shares,
                'current_price': current_price,
                'market_value': market_value,
                'ytd_dividends': div_data['total'],
                'payment_count': div_data['count'],
                'annual_dividend': annual_dividend,
                'dividend_yield': dividend_yield,
            })

        # Sort by YTD dividends descending
        dividend_stocks.sort(key=lambda x: x['ytd_dividends'], reverse=True)

        # Get bond interest data
        monthly_bond_interest = portfolio.get_monthly_bond_interest()
        bond_interest_by_symbol = portfolio.get_bond_interest_by_symbol()
        total_bond_interest = portfolio.total_bond_interest

        # Calculate average monthly income (dividends + bond interest)
        # Only count months from the current year for YTD average
        current_year = str(datetime.now().year)
        total_ytd = portfolio.total_dividends
        total_income = total_ytd + total_bond_interest
        all_income_months = set(monthly_dividends.keys()) | set(monthly_bond_interest.keys())
        # Filter to current year only
        ytd_months = {m for m in all_income_months if m.startswith(current_year)}
        if ytd_months:
            months_for_avg = len(ytd_months)
            avg_monthly_income = total_income / months_for_avg
        else:
            avg_monthly_income = 0

        # Build bond info list
        bond_list = []
        for symbol, interest_data in bond_interest_by_symbol.items():
            bond_list.append({
                'symbol': symbol,
                'bond_name': interest_data['bond_name'],
                'ytd_interest': interest_data['total'],
                'payment_count': interest_data['count'],
            })
        # Sort by YTD interest descending
        bond_list.sort(key=lambda x: x['ytd_interest'], reverse=True)

        chart_data = {
            'months': months,
            'months_count': len(months),
            'dividends': [monthly_dividends.get(m, 0) for m in months],
            'total_dividends': total_ytd,
            'avg_monthly_income': avg_monthly_income,
            'total_bond_interest': total_bond_interest,
            'bond_interest': [monthly_bond_interest.get(m, 0) for m in months],
        }

        return render_template('dividends.html',
                             portfolio=portfolio,
                             chart_data=chart_data,
                             dividend_stocks=dividend_stocks,
                             bond_list=bond_list,
                             portfolios=app.portfolios,
                             portfolio_filter=session.get('portfolio_filter', 'all'))

    @app.route('/api/income-projection')
    def api_income_projection():
        """API endpoint for income projection data (fetched async)."""
        from src.analysis.income_projection import calculate_income_projection

        portfolio, _ = get_active_portfolio()
        if portfolio is None:
            return jsonify({'error': 'No portfolio loaded'}), 404

        # Get monthly dividend data for months count
        monthly_dividends = portfolio.get_monthly_dividends()
        months = sorted(monthly_dividends.keys())

        # Get dividends by symbol
        dividends_by_symbol = portfolio.get_dividends_by_symbol()

        # Build dividend stocks list
        dividend_stocks = []
        for symbol, div_data in dividends_by_symbol.items():
            matching_stocks = [s for s in portfolio.stocks if s.symbol == symbol]
            shares = sum(s.quantity for s in matching_stocks) if matching_stocks else 0
            if shares <= 0:
                continue
            current_price = matching_stocks[0].current_price if matching_stocks else 0
            dividend_stocks.append({
                'symbol': symbol,
                'shares': shares,
                'current_price': current_price,
                'ytd_dividends': div_data['total'],
            })

        # Build bond list
        bond_interest_by_symbol = portfolio.get_bond_interest_by_symbol()
        bond_list = []
        for symbol, interest_data in bond_interest_by_symbol.items():
            bond_list.append({
                'symbol': symbol,
                'bond_name': interest_data['bond_name'],
                'ytd_interest': interest_data['total'],
                'payment_count': interest_data['count'],
            })

        # Calculate projection
        projection = calculate_income_projection(
            dividend_stocks=dividend_stocks,
            bond_list=bond_list,
            months_of_data=len(months) if months else 12
        )

        # Build monthly data arrays for chart
        month_names = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
        monthly_dividends = [projection.monthly_breakdown[m]['dividends'] for m in range(1, 13)]
        monthly_bonds = [projection.monthly_breakdown[m]['bonds'] for m in range(1, 13)]
        monthly_growth = [projection.monthly_breakdown[m]['growth_bonus'] for m in range(1, 13)]

        return jsonify({
            'total_dividend': projection.total_dividend_projection,
            'total_dividend_with_growth': projection.total_dividend_with_growth,
            'total_bond': projection.total_bond_projection,
            'total_income': projection.total_income_projection,
            'total_income_with_growth': projection.total_income_with_growth,
            'avg_cagr': projection.avg_dividend_cagr * 100,
            'monthly': {
                'months': month_names,
                'dividends': monthly_dividends,
                'bonds': monthly_bonds,
                'growth_bonus': monthly_growth,
            },
            'stocks': [
                {
                    'symbol': p.symbol,
                    'shares': p.shares,
                    'forward_rate': p.forward_dividend_rate,
                    'projected': p.projected_annual,
                    'yield': p.dividend_yield,
                    'cagr': p.cagr_5yr * 100 if p.cagr_5yr else None,
                    'projected_with_growth': p.projected_with_growth,
                    'frequency': p.frequency,
                    'payment_months': p.payment_months,
                }
                for p in projection.stock_projections
            ],
            'bonds': [
                {
                    'symbol': p.symbol,
                    'bond_name': p.bond_name,
                    'coupon_rate': p.coupon_rate,
                    'annual_interest': p.annual_interest,
                    'payment_months': p.payment_months,
                }
                for p in projection.bond_projections
            ],
        })

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

    @app.route('/api/debug/dividends')
    def debug_dividends():
        """Debug endpoint to inspect raw dividend data."""
        portfolio, _ = get_active_portfolio()
        if portfolio is None:
            return jsonify({'error': 'No portfolio loaded'}), 404

        dividends = []
        filtered_dividends = []
        for div in portfolio.dividends:
            div_info = {
                'symbol': div.symbol,
                'payment_date': div.payment_date.isoformat(),
                'amount': div.amount,
                'description': div.description[:50] if div.description else '',
                'in_period': portfolio._is_within_period(div.payment_date),
            }
            dividends.append(div_info)
            if div_info['in_period']:
                filtered_dividends.append(div_info)

        # Sort by date
        dividends.sort(key=lambda x: x['payment_date'])

        return jsonify({
            'statement_period': portfolio.statement_period,
            'period_start': portfolio.period_start.isoformat() if portfolio.period_start else None,
            'period_end': portfolio.period_end.isoformat() if portfolio.period_end else None,
            'total_dividends': len(dividends),
            'filtered_dividends': len(filtered_dividends),
            'dividends': dividends
        })

    return app

