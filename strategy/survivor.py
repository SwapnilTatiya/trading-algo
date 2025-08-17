import os
import sys
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import yaml
from logger import logger

class SurvivorStrategy:
    """
    Survivor Options Trading Strategy
    
    This strategy implements a systematic approach to options trading based on price movements
    of the NIFTY index. The core concept is to sell options (both PE and CE) when the underlying
    index moves beyond certain thresholds, capturing premium decay while managing risk through
    dynamic gap adjustments.
    
    STRATEGY OVERVIEW:
    ==================
    
    1. **Dual-Side Trading**: The strategy monitors both upward and downward movements:
       - PE (Put) Trading: Triggered when NIFTY price moves UP beyond pe_gap threshold
       - CE (Call) Trading: Triggered when NIFTY price moves DOWN beyond ce_gap threshold
    
    2. **Gap-Based Execution**: 
       - Maintains reference points (nifty_pe_last_value, nifty_ce_last_value)
       - Executes trades when price deviates beyond configured gaps
       - Uses multipliers to scale position sizes based on gap magnitude
    
    3. **Dynamic Strike Selection**:
       - Selects option strikes based on symbol_gap from current price
       - Adjusts strikes if option premium is below minimum threshold
       - Ensures adequate liquidity and pricing
    
    4. **Reset Mechanism**:
       - Automatically adjusts reference points when market moves favorably
       - Prevents excessive accumulation of positions
       - Maintains strategy responsiveness to market conditions
    
    TRADING LOGIC EXAMPLE:
    =====================
    
    Scenario: NIFTY at 24,500, pe_gap=25, pe_symbol_gap=200
    
    1. Initial State: nifty_pe_last_value = 24,500
    2. NIFTY rises to 24,530 (difference = 30)
    3. Since 30 > pe_gap(25), trigger PE sell
    4. Sell multiplier = 30/25 = 1 (rounded down)
    5. Select PE strike at 24,500-200 = 24,300 PE
    6. Update reference: nifty_pe_last_value = 24,525 (24,500 + 25*1)
    
    CONFIGURATION PARAMETERS:
    ========================
    
    Core Parameters:
    - symbol_initials: Option series identifier (e.g., 'NIFTY25JAN30')
    - index_symbol: Underlying index for tracking (e.g., 'NSE:NIFTY 50')
    
    Gap Parameters:
    - pe_gap/ce_gap: Price movement thresholds to trigger trades
    - pe_symbol_gap/ce_symbol_gap: Strike distance from current price
    - pe_reset_gap/ce_reset_gap: Favorable movement thresholds for reference reset
    
    Quantity & Risk:
    - pe_quantity/ce_quantity: Base quantities for each trade
    - min_price_to_sell: Minimum option premium threshold
    - sell_multiplier_threshold: Maximum position scaling limit
    
    RISK MANAGEMENT:
    ===============
    
    1. **Premium Filtering**: Only sells options above min_price_to_sell
    2. **Position Scaling**: Limits multiplier to prevent oversized positions
    3. **Strike Adjustment**: Dynamically adjusts strikes for adequate premium
    4. **Reset Logic**: Prevents runaway reference point drift

    PS: This will only work with Zerodha broker out of the box. For Fyers, there needs to be some straight forward changes to get quotes, place orders etc.
    """
    
    def __init__(self, broker, config, order_manager):
        # Assign config values as instance variables with 'strat_var_' prefix
        for k, v in config.items():
            setattr(self, f'strat_var_{k}', v)
        # External dependencies
        self.broker = broker
        self.symbol_initials = self.strat_var_symbol_initials
        self.order_manager = order_manager  # Store OrderTracker
        self.broker.download_instruments()
        
        self._initialize_state()

    def _nifty_quote(self):
        symbol_code = "NSE:NIFTY 50"
        return self.broker.get_quote(symbol_code, "NSE")

    def _initialize_state(self):

        # Initialize reset flags - these track when reset conditions are triggered
        self.pe_reset_gap_flag = 0  # Set to 1 when PE trade is executed
        self.ce_reset_gap_flag = 0  # Set to 1 when CE trade is executed
        
        # Get current market data for initialization
        current_quote = self._nifty_quote()
        print(current_quote)  # Debug output
        
        # Initialize PE reference value
        if self.strat_var_pe_start_point == 0:
            # Use current market price as starting reference
            self.nifty_pe_last_value = current_quote['last_price']
            logger.debug(f"Nifty PE Start Point is 0, so using LTP: {self.nifty_pe_last_value}")
        else:
            # Use configured starting point
            self.nifty_pe_last_value = self.strat_var_pe_start_point

        # Initialize CE reference value
        if self.strat_var_ce_start_point == 0:
            # Use current market price as starting reference
            self.nifty_ce_last_value = current_quote['last_price']
            logger.debug(f"Nifty CE Start Point is 0, so using LTP: {self.nifty_ce_last_value}")
        else:
            # Use configured starting point
            self.nifty_ce_last_value = self.strat_var_ce_start_point
            
        logger.info(f"Nifty PE Start Value during initialization: {self.nifty_pe_last_value}, "
                   f"Nifty CE Start Value during initialization: {self.nifty_ce_last_value}")

    def on_ticks_update(self, ticks):
        """
        Main strategy execution method called on each tick update
        
        Args:
            ticks (dict): Market data containing 'last_price' and other tick information
            
        This is the core method that:
        1. Extracts current price from tick data
        2. Evaluates PE trading opportunities
        3. Evaluates CE trading opportunities  
        4. Applies reset logic for reference values
        
        Called externally by the main trading loop when new market data arrives
        """
        current_price = ticks['last_price']
        
        # Process trading opportunities for both sides
        self._handle_pe_trade(current_price)  # Handle Put option opportunities
        self._handle_ce_trade(current_price)  # Handle Call option opportunities
        
        # Apply reset logic to adjust reference values
        self._reset_reference_values(current_price)

    def _check_sell_multiplier_breach(self, sell_multiplier):
        """
        Risk management check for position scaling
        
        Args:
            sell_multiplier (int): The calculated multiplier for position sizing
            
        Returns:
            bool: True if multiplier exceeds threshold, False otherwise
            
        This prevents excessive position sizes when large price movements occur.
        For example, if threshold is 3 and price moves 100 points with gap=25,
        multiplier would be 4, which exceeds threshold and blocks the trade.
        """
        if sell_multiplier > self.strat_var_sell_multiplier_threshold:
            logger.warning(f"Sell multiplier {sell_multiplier} breached the threshold {self.strat_var_sell_multiplier_threshold}")
            return True
        return False

    def _handle_pe_trade(self, current_price):
        """
        Handle PE (Put) option trading logic
        
        Args:
            current_price (float): Current NIFTY index price
            
        PE Trading Logic:
        - Triggered when current_price > nifty_pe_last_value + pe_gap
        - Sells PE options (benefits from upward price movement)
        - Updates reference value after execution
        
        Process:
        1. Check if upward movement exceeds gap threshold
        2. Calculate sell multiplier based on gap magnitude
        3. Validate multiplier doesn't breach risk limits
        4. Find appropriate PE strike with adequate premium
        5. Execute trade and update reference value
        
        Example:
        - Reference: 24,500, Gap: 25, Current: 24,560
        - Difference: 60, Multiplier: 60/25 = 2
        - Sell 2x PE quantity, Update reference to 24,550
        """
        # No action needed if price hasn't moved up sufficiently
        if current_price <= self.nifty_pe_last_value:
            self._log_stable_market(current_price)
            return

        # Calculate price difference and check if it exceeds gap threshold
        price_diff = round(current_price - self.nifty_pe_last_value, 0)
        if price_diff > self.strat_var_pe_gap:
            # Calculate multiplier for position sizing
            sell_multiplier = int(price_diff / self.strat_var_pe_gap)
            
            # Risk check: Ensure multiplier doesn't exceed threshold
            if self._check_sell_multiplier_breach(sell_multiplier):
                logger.warning(f"Sell multiplier {sell_multiplier} breached the threshold {self.strat_var_sell_multiplier_threshold}")
                return

            # Update reference value based on executed gaps
            self.nifty_pe_last_value += self.strat_var_pe_gap * sell_multiplier
            
            # Calculate total quantity to trade
            total_quantity = sell_multiplier * self.strat_var_pe_quantity

            # Find suitable PE option with adequate premium
            temp_gap = self.strat_var_pe_symbol_gap
            while True:
                # Find PE instrument at specified gap from current price
                instrument = self.broker.find_instrument(self.symbol_initials, "PE", current_price, gap=temp_gap)
                if not instrument:
                    logger.warning("No suitable instrument found for PE with gap %s", temp_gap)
                    return 
                
                # Get current quote for the selected instrument
                quote = self.broker.get_quote(instrument['tradingsymbol'], self.strat_var_exchange)
                
                # Check if premium meets minimum threshold
                if quote['last_price'] < self.strat_var_min_price_to_sell:
                    logger.info(f"Last price {quote['last_price']} is less than min price to sell {self.strat_var_min_price_to_sell}")
                    # Try closer strike if premium is too low
                    temp_gap -= self.strat_var_nifty_lot_size
                    continue
                    
                # Execute the trade
                logger.info(f"Execute PE sell @ {instrument['tradingsymbol']} × {total_quantity}, Market Price")
                self._place_order(instrument['tradingsymbol'], total_quantity)
                
                # Set reset flag to enable reset logic
                self.pe_reset_gap_flag = 1
                break

    def _handle_ce_trade(self, current_price):
        """
        Handle CE (Call) option trading logic
        
        Args:
            current_price (float): Current NIFTY index price
            
        CE Trading Logic:
        - Triggered when current_price < nifty_ce_last_value - ce_gap
        - Sells CE options (benefits from downward price movement)
        - Updates reference value after execution
        
        Process:
        1. Check if downward movement exceeds gap threshold
        2. Calculate sell multiplier based on gap magnitude
        3. Validate multiplier doesn't breach risk limits
        4. Find appropriate CE strike with adequate premium
        5. Execute trade and update reference value
        
        Example:
        - Reference: 24,500, Gap: 25, Current: 24,440
        - Difference: 60, Multiplier: 60/25 = 2
        - Sell 2x CE quantity, Update reference to 24,450
        """
        # No action needed if price hasn't moved down sufficiently
        if current_price >= self.nifty_ce_last_value:
            self._log_stable_market(current_price)
            return

        # Calculate price difference and check if it exceeds gap threshold
        price_diff = round(self.nifty_ce_last_value - current_price, 0)  
        if price_diff > self.strat_var_ce_gap:
            # Calculate multiplier for position sizing
            sell_multiplier = int(price_diff / self.strat_var_ce_gap)
            
            # Risk check: Ensure multiplier doesn't exceed threshold
            if self._check_sell_multiplier_breach(sell_multiplier):
                logger.warning(f"Sell multiplier {sell_multiplier} breached the threshold {self.strat_var_sell_multiplier_threshold}")
                return

            # Update reference value based on executed gaps
            self.nifty_ce_last_value -= self.strat_var_ce_gap * sell_multiplier
            
            # Calculate total quantity to trade
            total_quantity = sell_multiplier * self.strat_var_ce_quantity

            # Find suitable CE option with adequate premium
            temp_gap = self.strat_var_ce_symbol_gap 
            while True:
                # Find CE instrument at specified gap from current price
                instrument = self.broker.find_instrument(self.symbol_initials, "CE", current_price, gap=temp_gap)
                if not instrument:
                    logger.warning("No suitable instrument found for CE with gap %s", temp_gap)
                    return
                    
                # Get current quote for the selected instrument
                quote = self.broker.get_quote(instrument['tradingsymbol'], self.strat_var_exchange)
                
                # Check if premium meets minimum threshold
                if quote['last_price'] < self.strat_var_min_price_to_sell:
                    logger.info(f"Last price {quote['last_price']} is less than min price to sell {self.strat_var_min_price_to_sell}, trying next strike")
                    # Try closer strike if premium is too low
                    temp_gap -= self.strat_var_nifty_lot_size
                    continue
                    
                # Execute the trade
                logger.info(f"Execute CE sell @ {instrument['tradingsymbol']} × {total_quantity}, Market Price")
                self._place_order(instrument['tradingsymbol'], total_quantity)
                
                # Set reset flag to enable reset logic
                self.ce_reset_gap_flag = 1
                break

    def _reset_reference_values(self, current_price):
        """
        Reset reference values when market moves favorably
        
        Args:
            current_price (float): Current NIFTY index price
            
        Reset Logic:
        - PE Reset: When price drops significantly below PE reference AND reset flag is set
        - CE Reset: When price rises significantly above CE reference AND reset flag is set
        
        Purpose:
        1. Prevents reference values from drifting too far from market
        2. Maintains strategy responsiveness to changing market conditions
        3. Reduces risk of excessive position accumulation
        
        Reset Conditions:
        - PE: (pe_last_value - current_price) > pe_reset_gap AND pe_reset_gap_flag = 1
        - CE: (current_price - ce_last_value) > ce_reset_gap AND ce_reset_gap_flag = 1
        
        Example PE Reset:
        - PE Reference: 24,550, Current: 24,480, Reset Gap: 50
        - Difference: 70 > 50, so reset PE reference to 24,530 (24,480 + 50)
        """
        # PE Reset Logic: Reset when price drops significantly below PE reference
        if (self.nifty_pe_last_value - current_price) > self.strat_var_pe_reset_gap and self.pe_reset_gap_flag:
            logger.info(f"Resetting PE value from {self.nifty_pe_last_value} to {current_price + self.strat_var_pe_reset_gap}")
            # Reset PE reference to current price plus reset gap
            self.nifty_pe_last_value = current_price + self.strat_var_pe_reset_gap

        # CE Reset Logic: Reset when price rises significantly above CE reference  
        if (current_price - self.nifty_ce_last_value) > self.strat_var_ce_reset_gap and self.ce_reset_gap_flag:
            logger.info(f"Resetting CE value from {self.nifty_ce_last_value} to {current_price - self.strat_var_ce_reset_gap}")
            # Reset CE reference to current price minus reset gap
            self.nifty_ce_last_value = current_price - self.strat_var_ce_reset_gap

    def _find_price_eligible_symbol(self, option_type):
        """
        Find an option symbol that meets premium requirements
        
        Args:
            option_type (str): 'PE' or 'CE'
            
        Returns:
            dict: Instrument details for eligible option, or None if none found
            
        This method iteratively searches for options that:
        1. Meet the gap criteria
        2. Have premium above minimum threshold
        3. Are liquid and tradeable
        
        Note: This method appears to have some issues and may not be actively used
        in the current implementation. The main trading methods use inline logic instead.
        """
        # Get initial gap based on option type
        temp_gap = self.strat_var_pe_symbol_gap if option_type == "PE" else self.strat_var_ce_symbol_gap
        
        while True:
            # Get current market price
            ltp = self._nifty_quote()['last_price']
            
            # Find instrument at current gap
            instrument = self.broker.find_instrument(self.symbol_initials, option_type, ltp, gap=temp_gap)
            
            if instrument is None:
                return None
                
            # Check if premium meets minimum threshold
            price = self.broker.get_quote(instrument['tradingsymbol'], self.strat_var_exchange)['last_price']
            
            if price < self.strat_var_min_price_to_sell:
                # Try closer strike if premium too low
                temp_gap -= self.strat_var_nifty_lot_size
            else:
                return instrument

    def _place_order(self, symbol, quantity):
        """
        Execute order placement through the broker
        
        Args:
            symbol (str): Trading symbol for the option
            quantity (int): Number of lots/shares to trade
            
        Process:
        1. Place market order through broker interface
        2. Log order details
        3. Track order in order management system
        4. Handle order failures gracefully
        
        Order Parameters:
        - Transaction Type: From configuration (typically SELL)
        - Order Type: From configuration (typically MARKET)
        - Exchange: From configuration (typically NFO)
        - Product: From configuration (NRML/MIS)
        - Variety: Always REGULAR
        - Tag: "Survivor" for identification
        """
        # Place order through broker interface
        order_id = self.broker.place_order(
            symbol, 
            quantity, 
            price=None,  # Market order
            transaction_type=self.strat_var_trans_type, 
            order_type=self.strat_var_order_type, 
            variety="REGULAR", 
            exchange=self.strat_var_exchange, 
            product=self.strat_var_product_type, 
            tag="Survivor"
        )
        
        # Handle order placement failure
        if order_id == -1:
            logger.error(f"Order placement failed for {symbol} × {quantity}, Market Price")
            return
            
        logger.info(f"Placing order for {symbol} × {quantity}, Market Price")
        
        # Track the order using OrderTracker
        from datetime import datetime
        order_details = {
            "order_id": order_id,
            "symbol": symbol,
            "transaction_type": self.strat_var_trans_type,
            "quantity": quantity,
            "price": None,  # Market order
            "timestamp": datetime.now().isoformat(),
        }
        
        # Add to order tracking system
        self.order_manager.add_order(order_details)
        

    def _log_stable_market(self, current_val):
        """
        Log current market state when no trading action is taken

        """
        logger.info(
            f"{self.strat_var_symbol_initials} Nifty under control. "
            f"PE = {self.nifty_pe_last_value}, "
            f"CE = {self.nifty_ce_last_value}, "
            f"Current = {current_val}, "
            f"CE Gap = {self.strat_var_ce_gap}, "
            f"PE Gap = {self.strat_var_pe_gap}"
        )