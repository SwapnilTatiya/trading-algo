
import os, sys
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from typing import Dict, Any, Optional, List
import requests
import pandas as pd
from dotenv import load_dotenv
from brokers.base import BrokerBase
from SmartApi import SmartConnect, SmartWebSocket
import pyotp

from logger import logger


load_dotenv()


# --- Angel Broking Broker ---
class AngelBroker(BrokerBase):
    def __init__(self):
        super().__init__()
        self.smart_api, self.auth_response_data = self.authenticate()
        self.ws = None
        self.symbols = []
        self.instruments_df = None
        self.download_instruments()
        
    def authenticate(self):
        api_key = os.getenv('BROKER_API_KEY')
        client_id = os.getenv('BROKER_ID')
        password = os.getenv('BROKER_PASSWORD')
        totp_secret = os.getenv('BROKER_TOTP_KEY')

        if not all([api_key, client_id, password, totp_secret]):
            raise Exception("Missing one or more required environment variables for Angel Broking.")

        smart_api = SmartConnect(api_key)
        
        # Generate TOTP
        try:
            totp = pyotp.TOTP(totp_secret).now()
        except Exception as e:
            raise Exception(f"Invalid TOTP secret: {e}")

        # Login
        data = {
            "clientID": client_id,
            "password": password,
            "totp": totp
        }
        
        auth_response = smart_api.generateSession(data['clientID'], data['password'], data['totp'])
        
        if auth_response.get("status") and auth_response.get("data"):
            logger.info("Authentication successful.")
            # self.smart_api.set_access_token(auth_response["data"]["jwtToken"])
            return smart_api, auth_response["data"]
        else:
            raise Exception(f"Authentication failed: {auth_response}")
    
    def download_instruments(self):
        try:
            instrument_url = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
            response = requests.get(instrument_url)
            instrument_list = response.json()
            self.instruments_df = pd.DataFrame(instrument_list)
            logger.info("Instruments downloaded successfully.")
        except Exception as e:
            logger.error(f"Error downloading instruments: {e}")

    def get_symbol_token(self, exchange, symbol):
        if self.instruments_df is None:
            self.download_instruments()
        
        if self.instruments_df is not None:
            try:
                token = self.instruments_df[(self.instruments_df['exch_seg'] == exchange) & (self.instruments_df['symbol'] == symbol)]['token'].values[0]
                return token
            except IndexError:
                logger.error(f"Token not found for {symbol} in {exchange}")
                return None
        else:
            return None

    def get_orders(self):
        try:
            return self.smart_api.orderBook()
        except Exception as e:
            logger.error(f"Error getting orders: {e}")
            return None
    
    def get_quote(self, symbol, exchange):
        try:
            token = self.get_symbol_token(exchange, symbol)
            if token:
                quote = self.smart_api.ltpData(exchange, symbol, token)["data"]
                return {
                    "symbol": quote['symbol'],
                    "last_price": quote['ltp'],
                    "instrument_token": quote['symboltoken']
                }
            else:
                return None
        except Exception as e:
            logger.error(f"Error getting quote for {symbol}: {e}")
            return None
    
    def find_instrument(self, symbol_initials, option_type, ltp, gap):
        if self.instruments_df is None:
            self.download_instruments()

        if option_type == "PE":
            symbol_gap = -gap
        else:
            symbol_gap = gap
            
        target_strike = ltp + symbol_gap
        
        df = self.instruments_df[
            (self.instruments_df['name'] == symbol_initials.split("NIFTY")[0]) &
            (self.instruments_df['instrumenttype'] == "OPTIDX") &
            (self.instruments_df['exchangeseg'] == "NFO") &
            (self.instruments_df['symbol'].str.endswith(option_type))
        ]
        
        if df.empty:
            return None
            
        df['target_strike_diff'] = (df['strike'].astype(float) - target_strike).abs()
        
        tolerance = self._get_strike_difference(symbol_initials) / 2
        df = df[df['target_strike_diff'] <= tolerance]
        
        if df.empty:
            logger.error(f"No instrument found for {symbol_initials} {option_type} "
                        f"within {tolerance} of {target_strike}")
            return None
            
        best = df.sort_values('target_strike_diff').iloc[0]
        return best.to_dict()

    def _get_strike_difference(self, symbol_initials):
        if self.instruments_df is None:
            self.download_instruments()

        ce_instruments = self.instruments_df[
            (self.instruments_df['name'] == symbol_initials.split("NIFTY")[0]) &
            (self.instruments_df['instrumenttype'] == "OPTIDX") &
            (self.instruments_df['exchangeseg'] == "NFO") &
            (self.instruments_df['symbol'].str.endswith('CE'))
        ]
        
        if ce_instruments.shape[0] < 2:
            logger.error(f"Not enough CE instruments found for {symbol_initials} to calculate strike difference")
            return 0

        ce_instruments_sorted = ce_instruments.sort_values('strike')

        top2 = ce_instruments_sorted.head(2)

        return abs(float(top2.iloc[1]['strike']) - float(top2.iloc[0]['strike']))
    
    def place_gtt_order(self, symbol, quantity, price, transaction_type, order_type, exchange, product, tag="Unknown"):
        try:
            gtt_params = {
                "tradingsymbol": symbol,
                "symboltoken": self.get_symbol_token(exchange, symbol),
                "exchange": exchange,
                "producttype": product,
                "transactiontype": transaction_type,
                "price": price,
                "qty": quantity,
                "triggerprice": price,
                "disclosedqty": 0,
                "timeperiod": 365 # Validity in days
            }
            gtt_order_id = self.smart_api.gttCreateRule(gtt_params)
            logger.info(f"GTT Order placed: {gtt_order_id}")
            return gtt_order_id
        except Exception as e:
            logger.error(f"GTT Order placement failed: {e}")
            return -1

    def place_order(self, symbol, quantity, price, transaction_type, order_type, variety, exchange, product, tag="Unknown"):
        try:
            order_params = {
                "variety": variety,
                "tradingsymbol": symbol,
                "symboltoken": self.get_symbol_token(exchange, symbol),
                "transactiontype": transaction_type,
                "exchange": exchange,
                "ordertype": order_type,
                "producttype": product,
                "duration": "DAY",
                "price": price,
                "squareoff": "0",
                "stoploss": "0",
                "quantity": quantity
            }
            order_id = self.smart_api.placeOrder(order_params)
            logger.info(f"Order placed: {order_id}")
            return order_id
        except Exception as e:
            logger.error(f"Order placement failed: {e}")
            return -1
    
    def get_positions(self):
        try:
            return self.smart_api.position()
        except Exception as e:
            logger.error(f"Error getting positions: {e}")
            return None

    def symbols_to_subscribe(self, symbols):
        self.symbols = symbols

    ## Websocket Calllbacks
    def on_ticks(self, ws, ticks):
        logger.info("Ticks: {}".format(ticks))

    def on_connect(self, ws, response):
        logger.info("Connected")
        token = ""
        for symbol in self.symbols:
            exchange = symbol.split(':')[0]
            instrument = symbol.split(':')[1]
            if exchange == "NSE":
                exchange = "nse_cm"
            elif exchange == "NFO":
                exchange = "nse_fo"
            token += f"{exchange}|{self.get_symbol_token(exchange, instrument)}&"
        
        self.ws.subscribe("mw", token[:-1])


    def on_close(self, ws, code, reason):
        logger.info("Connection closed: {code} - {reason}".format(code=code, reason=reason))

    def on_error(self, ws, code, reason):
        logger.info("Connection error: {code} - {reason}".format(code=code, reason=reason))

    def connect_websocket(self):
        auth_token = self.auth_response_data.get("jwtToken")
        api_key = os.getenv('BROKER_API_KEY')
        client_code = os.getenv('BROKER_ID')
        
        if not all([auth_token, api_key, client_code]):
            raise Exception("Missing required data for websocket connection.")

        self.ws = SmartWebSocket(auth_token, api_key, client_code, self.auth_response_data.get("feedToken"))
        
        self.ws.on_ticks = self.on_ticks
        self.ws.on_connect = self.on_connect
        self.ws.on_close = self.on_close
        self.ws.on_error = self.on_error
        
        self.ws.connect()
