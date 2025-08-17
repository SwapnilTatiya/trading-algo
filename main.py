if __name__ == "__main__":
    import time
    import yaml
    import sys
    import argparse
    from dispatcher import DataDispatcher
    from orders import OrderTracker
    from strategy.survivor import SurvivorStrategy
    from brokers.zerodha import ZerodhaBroker
    from brokers.angel import AngelBroker
    from brokers.fyers import FyersBroker
    from logger import logger
    from queue import Queue
    import random
    import traceback
    import warnings
    warnings.filterwarnings("ignore")
    import os

    import logging
    logger.setLevel(logging.INFO)
    
    # ==========================================================================
    # SECTION 1: CONFIGURATION LOADING AND PARSING
    # ==========================================================================
    
    # Load default configuration from YAML file
    config_file = os.path.join(os.path.dirname(__file__), "strategy/configs/survivor.yml")
    with open(config_file, 'r') as f:
        config = yaml.safe_load(f)['default']

    def create_argument_parser():
        """Create and configure argument parser with detailed help"""
        parser = argparse.ArgumentParser(
            description="Survivor",
            formatter_class=argparse.RawDescriptionHelpFormatter,
            epilog="""

    Examples:
    # Use default configuration from survivor.yml
    python system/main.py
    
    # Override specific parameters
    python system/main.py --symbol-initials NIFTY25807 --pe-gap 25 --ce-gap 25
    
    # Full parameter override
    python system/main.py \
        --symbol-initials NIFTY25807 \
        --pe-symbol-gap 200 --ce-symbol-gap 200 \
        --exchange NFO \
        --pe-gap 20 --ce-gap 20 \
        --pe-reset-gap 30 --ce-reset-gap 30 \
        --pe-quantity 50 --ce-quantity 50 \
        --pe-start-point 0 --ce-start-point 0 \
        --order-type MARKET --product-type NRML \
        --min-price-to-sell 15 --trans-type SELL

CONFIGURATION HIERARCHY:
=======================
1. Command line arguments (highest priority)
2. survivor.yml default values (fallback)

PARAMETER GROUPS:
================
• Core Parameters: symbol-initials, index-symbol
• Gap Parameters: pe-gap, ce-gap, pe-reset-gap, ce-reset-gap  
• Strike Selection: pe-symbol-gap, ce-symbol-gap
• Order Management: order-type, product-type, exchange
• Risk Management: min-price-to-sell, sell-multiplier-threshold
• Position Sizing: pe-quantity, ce-quantity
            """)
        
        # =======================================================================
        # CORE TRADING PARAMETERS
        # =======================================================================
        
        parser.add_argument('--symbol-initials', type=str,
                        help='Option series identifier (e.g., NIFTY25JAN30). '
                             'Must be at least 9 characters. This identifies the specific '
                             'option expiry series to trade.')
        
        parser.add_argument('--index-symbol', type=str,
                        help='Underlying index symbol for price tracking (e.g., NSE:NIFTY 50). '
                             'This is the reference index whose price movements trigger trades.')
        
        # =======================================================================
        # STRIKE SELECTION PARAMETERS  
        # =======================================================================
        
        parser.add_argument('--pe-symbol-gap', type=int,
                        help='Distance below current price for PE strike selection. '
                             'E.g., if NIFTY is at 24500 and pe-symbol-gap is 200, '
                             'PE strikes around 24300 will be selected.')
        
        parser.add_argument('--ce-symbol-gap', type=int,
                        help='Distance above current price for CE strike selection. '
                             'E.g., if NIFTY is at 24500 and ce-symbol-gap is 200, '
                             'CE strikes around 24700 will be selected.')
        
        # =======================================================================
        # EXCHANGE AND ORDER SETTINGS
        # =======================================================================
        
        parser.add_argument('--exchange', type=str, choices=['NFO'],
                        help='Exchange for trading (NFO for F&O, NSE for equity)')
        parser.add_argument('--order-type', type=str, choices=['MARKET', 'LIMIT'],
                        help='Order type for placing trades')
        parser.add_argument('--product-type', type=str, choices=['NRML'],
                        help='Product type for orders')
        
        # =======================================================================
        # GAP PARAMETERS FOR TRADE TRIGGERING
        # =======================================================================
        
        parser.add_argument('--pe-gap', type=float,
                        help='NIFTY upward movement threshold to trigger PE sells. '
                             'E.g., if pe-gap is 25 and NIFTY moves up 30 points, '
                             'PE options will be sold (multiplier = 30/25 = 1).')
        
        parser.add_argument('--ce-gap', type=float,
                        help='NIFTY downward movement threshold to trigger CE sells. '
                             'E.g., if ce-gap is 25 and NIFTY moves down 30 points, '
                             'CE options will be sold (multiplier = 30/25 = 1).')
        
        # =======================================================================
        # RESET GAP PARAMETERS
        # =======================================================================
        
        parser.add_argument('--pe-reset-gap', type=float,
                        help='Favorable movement threshold to reset PE reference value. '
                             'When NIFTY moves down by this amount after PE trades, '
                             'the PE reference is reset closer to current price.')
        
        parser.add_argument('--ce-reset-gap', type=float,
                        help='Favorable movement threshold to reset CE reference value. '
                             'When NIFTY moves up by this amount after CE trades, '
                             'the CE reference is reset closer to current price.')
        
        # =======================================================================
        # QUANTITY PARAMETERS
        # =======================================================================
        
        parser.add_argument('--pe-quantity', type=int,
                        help='Base quantity for PE option trades. Total quantity = '
                             'pe-quantity × sell-multiplier. E.g., if pe-quantity=50 '
                             'and multiplier=2, total PE quantity = 100.')
        
        parser.add_argument('--ce-quantity', type=int,
                        help='Base quantity for CE option trades. Total quantity = '
                             'ce-quantity × sell-multiplier. E.g., if ce-quantity=50 '
                             'and multiplier=2, total CE quantity = 100.')
        
        # =======================================================================
        # STARTING REFERENCE POINTS
        # =======================================================================
        
        parser.add_argument('--pe-start-point', type=int,
                        help='Initial PE reference value. If 0, uses current market price. '
                             'If specified, uses that value as starting reference. '
                             'E.g., --pe-start-point 24500 starts PE tracking from 24500.')
        
        parser.add_argument('--ce-start-point', type=int,
                        help='Initial CE reference value. If 0, uses current market price. '
                             'If specified, uses that value as starting reference. '
                             'E.g., --ce-start-point 24500 starts CE tracking from 24500.')
        
        # =======================================================================
        # RISK MANAGEMENT PARAMETERS
        # =======================================================================
        
        parser.add_argument('--trans-type', type=str, choices=['BUY', 'SELL'],
                        help='Transaction type for all orders. Typically SELL for '
                             'premium collection strategies like this one.')
        
        parser.add_argument('--min-price-to-sell', type=float,
                        help='Minimum option premium threshold for execution. Options '
                             'with premium below this value will be skipped. Prevents '
                             'trading illiquid or very cheap options.')
        
        parser.add_argument('--sell-multiplier-threshold', type=float,
                        help='Maximum allowed position multiplier. Prevents excessive '
                             'position sizes during large market moves. E.g., if threshold '
                             'is 3 and calculated multiplier is 4, trade will be blocked.')
        
        # =======================================================================
        # UTILITY OPTIONS
        # =======================================================================
        
        parser.add_argument('--show-config', action='store_true',
                        help='Display current configuration (after applying overrides) and exit. '
                             'Useful for verifying parameter values before trading.')
        
        parser.add_argument('--config-file', type=str, default=config_file,
                        help='Path to YAML configuration file containing default values. '
                             'Defaults to system/strategy/configs/survivor.yml')
        
        return parser

    def show_config(config):
        """
        Display current configuration in organized format
        
        Args:
            config (dict): Configuration dictionary to display
            
        """
        print("\n" + "="*80)
        print("SURVIVOR STRATEGY CONFIGURATION")
        print("="*80)
        
        # Group parameters by functionality for better readability
        sections = {
            "Index & Symbol Configuration": [
                'index_symbol', 'symbol_initials'
            ],
            "Exchange & Order Management": [
                'exchange', 'order_type', 'product_type', 'trans_type'
            ],
            "Gap Parameters (Trade Triggers)": [
                'pe_gap', 'ce_gap', 'pe_reset_gap', 'ce_reset_gap'
            ],
            "Strike Selection (Distance from Spot)": [
                'pe_symbol_gap', 'ce_symbol_gap'
            ],
            "Position Sizing": [
                'pe_quantity', 'ce_quantity'
            ],
            "Reference Points (Starting Values)": [
                'pe_start_point', 'ce_start_point'
            ],
            "Risk Management": [
                'min_price_to_sell', 'sell_multiplier_threshold'
            ]
        }
        
        for section, fields in sections.items():
            print(f"\n{section}:")
            print("-" * len(section))
            for field in fields:
                value = config.get(field, 'NOT SET')
                # Add units/context for clarity
                unit_context = {
                    'pe_gap': 'points',
                    'ce_gap': 'points', 
                    'pe_reset_gap': 'points',
                    'ce_reset_gap': 'points',
                    'pe_symbol_gap': 'points from spot',
                    'ce_symbol_gap': 'points from spot',
                    'pe_quantity': 'units',
                    'ce_quantity': 'units',
                    'min_price_to_sell': 'rupees'
                }
                unit = unit_context.get(field, '')
                print(f"  {field:25}: {value} {unit}".strip())
        
        print("\n" + "="*80)
        print("TRADING LOGIC SUMMARY:")
        print("="*80)
        print(f"• PE Sells triggered when NIFTY rises >{config.get('pe_gap', 'N/A')} points")
        print(f"• CE Sells triggered when NIFTY falls >{config.get('ce_gap', 'N/A')} points") 
        print(f"• PE strikes selected ~{config.get('pe_symbol_gap', 'N/A')} points below spot")
        print(f"• CE strikes selected ~{config.get('ce_symbol_gap', 'N/A')} points above spot")
        print(f"• Minimum option premium: ₹{config.get('min_price_to_sell', 'N/A')}")
        print(f"• Maximum position multiplier: {config.get('sell_multiplier_threshold', 'N/A')}x")
        print("="*80)

    # ==========================================================================
    # SECTION 2: ARGUMENT PARSING AND CONFIGURATION MERGING
    # ==========================================================================
    
    # Parse command line arguments
    parser = create_argument_parser()
    args = parser.parse_args()

    # Define mapping between argument names and configuration keys
    # This allows clean separation between CLI argument naming conventions
    # and internal configuration parameter names
    arg_to_config_mapping = {
        'symbol_initials': 'symbol_initials',
        'index_symbol': 'index_symbol',
        'pe_symbol_gap': 'pe_symbol_gap',
        'ce_symbol_gap': 'ce_symbol_gap',
        'exchange': 'exchange',
        'order_type': 'order_type',
        'product_type': 'product_type',
        'pe_gap': 'pe_gap',
        'ce_gap': 'ce_gap',
        'pe_reset_gap': 'pe_reset_gap',
        'ce_reset_gap': 'ce_reset_gap',
        'pe_quantity': 'pe_quantity',
        'ce_quantity': 'ce_quantity',
        'pe_start_point': 'pe_start_point',
        'ce_start_point': 'ce_start_point',
        'trans_type': 'trans_type',
        'min_price_to_sell': 'min_price_to_sell',
        'sell_multiplier_threshold': 'sell_multiplier_threshold'
    }

    # Apply command line overrides to configuration
    # Priority: Command line args > YAML config > defaults
    overridden_params = []
    for arg_name, config_key in arg_to_config_mapping.items():
        # Convert dashes to underscores for argument attribute access
        arg_value = getattr(args, arg_name.replace('-', '_'))
        if arg_value is not None:
            config[config_key] = arg_value
            overridden_params.append(f"{config_key}={arg_value}")

    # Handle utility options
    if args.show_config:
        show_config(config)
        sys.exit(0)

    # ==========================================================================
    # SECTION 3: CONFIGURATION VALIDATION AND LOGGING
    # ==========================================================================
    
    # Validate that user has updated default configuration values
    def validate_configuration(config):
        """
        Validate that user has updated at least some default configuration values
        Returns True if config is valid, False otherwise
        """
        # Define default values that indicate user hasn't updated config
        default_values = {
            'symbol_initials': 'NIFTY25807',  
            'pe_gap': 20,
            'ce_gap': 20,
            'pe_quantity': 75,
            'ce_quantity': 75,
            'pe_symbol_gap': 200,
            'ce_symbol_gap': 200,
            'min_price_to_sell': 15,
            'pe_reset_gap': 30,
            'ce_reset_gap': 30,
            'pe_start_point': 0,
            'ce_start_point': 0,
            'sell_multiplier_threshold': 5
        }
        
        # Check which values are still at defaults
        unchanged_values = []
        changed_values = []
        for key, default_value in default_values.items():
            if config.get(key) == default_value:
                unchanged_values.append(key)
            else:
                changed_values.append(key)
        
        # If ALL values are still at defaults, show error and exit
        if len(changed_values) == 0:
            print("\n" + "="*80)
            print("❌ CONFIGURATION VALIDATION FAILED")
            print("="*80)
            print("ALL configuration values are still at their defaults!")
            print("You must update at least some parameters before running the strategy.")
            print()
            print("CRITICAL PARAMETERS TO UPDATE:")
            print("• symbol_initials: Must match current option series (e.g., NIFTY25JAN30)")
            print("• pe_gap/ce_gap: Price movement thresholds for your strategy")
            print("• pe_quantity/ce_quantity: Position sizes based on your capital")
            print("• min_price_to_sell: Minimum option premium threshold")
            print()
            print("Example command line usage:")
            print("python survivor.py")
            print("    --symbol-initials NIFTY25JAN30 ")
            print("    --pe-gap 25 --ce-gap 25 ")
            print("    --pe-quantity 50 --ce-quantity 50 ")
            print("    --min-price-to-sell 20")
            print("="*80)
            return False
        
        # If SOME values are still at defaults, show warning and ask for confirmation
        if len(unchanged_values) > 0:
            print("\n" + "="*80)
            print("⚠️  CONFIGURATION WARNING")
            print("="*80)
            print("Some configuration values are still at their defaults:")
            print()
            
            for value in unchanged_values:
                print(f"  ⚠️  {value}: {config.get(value)} (default)")
            
            if len(changed_values) > 0:
                print("\nUpdated values:")
                for value in changed_values:
                    print(f"  ✅ {value}: {config.get(value)} (updated)")
            
            print("\n" + "="*80)
            print("⚠️  WARNING: Running with default values may result in:")
            print("   • Trading wrong option series")
            print("   • Incorrect position sizes")
            print("   • Poor risk management")
            print("   • Potential losses")
            print("="*80)
            
            # Ask for user confirmation
            while True:
                response = input("\nDo you want to proceed with this configuration? (yes/no): ").lower().strip()
                if response in ['yes', 'y']:
                    print("\n✅ Proceeding with current configuration...")
                    return True
                elif response in ['no', 'n']:
                    print("\n❌ Strategy execution cancelled by user.")
                    print("Please update your configuration and try again.")
                    return False
                else:
                    print("Please enter 'yes' or 'no'.")
        
        # If all values have been updated, proceed without confirmation
        print("\n" + "="*80)
        print("✅ CONFIGURATION VALIDATION PASSED")
        print("="*80)
        print("All critical parameters have been updated from defaults.")
        print("Proceeding with strategy execution...")
        print("="*80)
        return True
    
    # Run configuration validation
    if not validate_configuration(config):
        logger.error("Configuration validation failed. Please update your configuration.")
        sys.exit(1)

    # Log configuration source and overrides
    if overridden_params:
        logger.info(f"Configuration loaded from {config_file} with command line overrides:")
        for param in overridden_params:
            logger.info(f"  Override: {param}")
    else:
        logger.info(f"Using default configuration from {config_file}")

    # Log key trading parameters for verification
    logger.info(f"Trading Configuration:")
    logger.info(f"  Symbol: {config['symbol_initials']}, Exchange: {config['exchange']}")
    logger.info(f"  Gap Triggers - PE: {config['pe_gap']}, CE: {config['ce_gap']}")
    logger.info(f"  Strike Selection - PE: -{config['pe_symbol_gap']}, CE: +{config['ce_symbol_gap']}")
    logger.info(f"  Base Quantities - PE: {config['pe_quantity']}, CE: {config['ce_quantity']}")
    logger.info(f"  Risk Limits - Min Premium: ₹{config['min_price_to_sell']}, Max Multiplier: {config['sell_multiplier_threshold']}x")

    # ==========================================================================
    # SECTION 4: TRADING INFRASTRUCTURE SETUP
    # ==========================================================================
    
    broker_name = os.getenv("BROKER_NAME", "zerodha").lower()
    broker = None

    if broker_name == "zerodha":
        if os.getenv("BROKER_TOTP_ENABLE") == "true":
            logger.info("Using Zerodha TOTP login flow")
            broker = ZerodhaBroker(without_totp=False)
        else:
            logger.info("Using Zerodha normal login flow")
            broker = ZerodhaBroker(without_totp=True)
    elif broker_name == "angel":
        logger.info("Using Angel One login flow")
        broker = AngelBroker()
    elif broker_name == "fyers":
        logger.info("Using Fyers login flow")
        broker = FyersBroker()
    else:
        raise Exception(f"Invalid broker name: {broker_name}")
    
    # Create order tracking system for position management
    order_tracker = OrderTracker() 
    
    # Get instrument token for the underlying index
    # This token is used for websocket subscription to receive real-time price updates
    try:
        quote_data = broker.get_quote(config['index_symbol'], config['exchange'])
        instrument_token = quote_data['instrument_token']
        logger.info(f"✓ Index instrument token obtained: {instrument_token}")
    except Exception as e:
        logger.error(f"Failed to get instrument token for {config['index_symbol']}: {e}")
        sys.exit(1)

    # Initialize data dispatcher for handling real-time market data
    # The dispatcher manages queues and routes market data to strategy
    dispatcher = DataDispatcher()
    dispatcher.register_main_queue(Queue())

    # ==========================================================================
    # SECTION 5: WEBSOCKET CALLBACK CONFIGURATION  
    # ==========================================================================
    
    # Define websocket event handlers for real-time data processing
    
    def on_ticks(ws, ticks):
        logger.debug("Received ticks: {}".format(ticks))
        # Send tick data to strategy processing queue
        dispatcher.dispatch(ticks)

    def on_connect(ws, response):
        logger.info("Websocket connected successfully: {}".format(response))
        
        # Subscribe to the underlying index instrument
        ws.subscribe([instrument_token])
        logger.info(f"✓ Subscribed to instrument token: {instrument_token}")
        
        # Set full mode to receive complete market data (LTP, volume, OI, etc.)
        ws.set_mode(ws.MODE_FULL, [instrument_token])

    def on_order_update(ws, data):
        logger.info("Order update received: {}".format(data))
        

    # Assign callbacks to broker's websocket instance
    broker.on_ticks = on_ticks
    broker.on_connect = on_connect
    broker.on_order_update = on_order_update

    # ==========================================================================
    # SECTION 6: STRATEGY INITIALIZATION AND WEBSOCKET START
    # ==========================================================================
    
    # Start websocket connection for real-time data
    broker.connect_websocket()

    # Initialize the trading strategy with all dependencies
    strategy = SurvivorStrategy(broker, config, order_tracker)

    # ==========================================================================
    # SECTION 7: MAIN TRADING LOOP
    # ==========================================================================
    
    try:
        while True:
            try:
                # STEP 1: Get market data from dispatcher queue
                # This call blocks until new tick data arrives from websocket
                tick_data = dispatcher._main_queue.get()
                
                # STEP 2: Extract the primary instrument data
                # tick_data is a list, we process the first instrument
                symbol_data = tick_data[0]
                
                # STEP 3: Optional data simulation for testing
                # You also need to move `tick_data = dispatcher._main_queue.get()` above 
                # outside of the while loop for this to work
                # if isinstance(symbol_data, dict) and 'last_price' in symbol_data:
                #     original_price = symbol_data['last_price']
                #     variation = random.uniform(-50, 50)  # ±50 point random variation
                #     symbol_data['last_price'] += variation
                #     logger.debug(f"Testing mode - Original: {original_price}, "
                #                 f"Modified: {symbol_data['last_price']} (Δ{variation:+.1f})")
                
                # STEP 4: Process tick through strategy
                # This triggers the main strategy logic for PE/CE evaluation
                strategy.on_ticks_update(symbol_data)
                
            except KeyboardInterrupt:
                # Handle graceful shutdown on Ctrl+C
                logger.info("SHUTDOWN REQUESTED - Stopping strategy...")
                break
                
            except Exception as tick_error:
                # Handle individual tick processing errors
                logger.error(f"Error processing tick data: {tick_error}")
                logger.error("Continuing with next tick...")
                # Continue the loop - don't stop for individual tick errors
                continue

    except Exception as fatal_error:
        # Handle fatal errors that require strategy shutdown
        logger.error("FATAL ERROR in main trading loop:")
        logger.error(f"Error: {fatal_error}")
        traceback.print_exc()
        
    finally:
        logger.info("STRATEGY SHUTDOWN COMPLETE")
