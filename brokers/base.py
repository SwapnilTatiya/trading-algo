import os
from typing import Dict, Any, Optional, List

class BrokerBase:
    """
    Minimal base class for brokers.
    Handles authentication and lists available functions.
    """
    def __init__(self):
        self.authenticated = False
        self.access_token = None
        self.env = os.environ

    def authenticate(self) -> Optional[str]:
        """
        Authenticate with the broker. To be implemented by subclasses.
        Returns access token if successful.
        """
        raise NotImplementedError("Subclasses must implement authenticate()")

    def get_quote(self, symbol, exchange) -> Dict[str, Any]:
        """
        Get quote for a symbol. To be implemented by subclasses.
        Should return a dictionary with the following structure:
        {
            "symbol": "...",
            "last_price": "...",
            "instrument_token": "..."
        }
        """
        raise NotImplementedError("Subclasses must implement get_quote()")

    def find_instrument(self, option_type, ltp, gap) -> Dict[str, Any]:
        """
        Find instrument for a given option type, ltp and gap. To be implemented by subclasses.
        """
        raise NotImplementedError("Subclasses must implement find_instrument()")

    def place_order(self, symbol, quantity, price, transaction_type, order_type, variety, exchange, product, tag="Unknown"):
        """
        Place an order. To be implemented by subclasses.
        """
        raise NotImplementedError("Subclasses must implement place_order()")

    def download_instruments(self):
        """
        Download instruments. To be implemented by subclasses.
        """
        raise NotImplementedError("Subclasses must implement download_instruments()")

    def list_functions(self) -> List[str]:
        """
        List available public methods (excluding private and base methods).
        """
        base_methods = set(dir(BrokerBase))
        all_methods = set(dir(self))
        public_methods = [m for m in all_methods - base_methods if not m.startswith('_')]
        return sorted(public_methods) 