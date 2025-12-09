#!/usr/bin/env python3
"""Run the Options Analyzer web application."""
import sys
import os

# Add src to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.web.app import create_app


def main():
    """Run the Flask development server."""
    app = create_app()
    
    # Get port from environment or use default
    port = int(os.environ.get('PORT', 5000))
    debug = os.environ.get('DEBUG', 'true').lower() == 'true'
    
    print(f"""
╔══════════════════════════════════════════════════════════════╗
║                    OPTIONS ANALYZER                          ║
╠══════════════════════════════════════════════════════════════╣
║  Server running at: http://localhost:{port}                    ║
║  Debug mode: {str(debug).ljust(46)}║
║                                                              ║
║  Press Ctrl+C to stop the server                             ║
╚══════════════════════════════════════════════════════════════╝
    """)
    
    app.run(host='0.0.0.0', port=port, debug=debug)


if __name__ == '__main__':
    main()

