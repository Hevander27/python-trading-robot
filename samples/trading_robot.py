
import time as time_lib
import pprint
import pathlib
import operator
import pandas as pd

from datetime import datetime
from datetime import timedelta
from configparser import ConfigParser

from pyrobot.robot import PyRobot
from pyrobot.indicators import Indicators

# Grab configuration values.
config = ConfigParser()
config.read('config/config.ini')

API_KEY = config.get('main', 'api_key')
APP_SECRET = config.get('main', 'app_secret')
CALLBACK_URL = config.get('main', 'callback_url')
TOKEN_PATH = config.get('main', 'token_path')
ACCOUNT_NUMBER = config.get('main', 'account_number')

# Initalize the robot.
trading_robot = PyRobot(
    api_key=API_KEY,
    app_secret=APP_SECRET,
    callback_url=CALLBACK_URL,
    token_path=TOKEN_PATH,
    trading_account=ACCOUNT_NUMBER,
    paper_trading=True
)

# Create a Portfolio
trading_robot_portfolio = trading_robot.create_portfolio()

# Define mutliple positions to add.
multi_position = [
    {
        
        'asset_type': 'equity',
        'quantity': 2,
        'purchase_price': 4.00,
        'symbol': 'TSLA',
        'purchase_date': '2020-01-31'
    },
    {
        'asset_type': 'equity',
        'quantity': 2,
        'purchase_price': 4.00,
        'symbol': 'SQ',
        'purchase_date': '2020-01-31'
    }
]

# Grab the New positions
new_positions = trading_robot.portfolio.add_positions(positions=multi_position)
pprint.pprint(new_positions)

# Add a single position
trading_robot_portfolio.add_position(
    symbol='MSFT',
    quantity=10,
    purchase_price=10,
    asset_type='equity',
    purchase_date='2020-04-01'
)

# Add another single position
trading_robot_portfolio.add_position(
    symbol='AAPL',
    quantity=10,
    purchase_price=10,
    asset_type='equity',
    purchase_date='2020-04-01'
)

# If the Market is open, print some quotes.
if trading_robot.regular_market_open:
    pprint.pprint(trading_robot.grab_current_quotes())

# If the Post Market is Open, do something.
elif trading_robot.post_market_open:
    pprint.pprint(trading_robot.grab_current_quotes())

# If the Pre Market is Open, do something.
elif trading_robot.pre_market_open:
    pprint.pprint(trading_robot.grab_current_quotes())

# Print the Positions
pprint.pprint(trading_robot_portfolio.positions)

# Grab the current quotes, for all of our positions.
current_quotes = trading_robot.grab_current_quotes()

# Print the Quotes.
pprint.pprint(current_quotes)

# Let's see if our Microsoft Position is profitable.
is_msft_porfitable = trading_robot.portfolio.is_profitable(
    symbol="MSFT",
    current_price=current_quotes['MSFT']['quote']['lastPrice']
)
print("Is Microsoft Profitable: {answer}".format(answer=is_msft_porfitable))

# Let's get the projected Market Value.
portfolio_summary = trading_robot.portfolio.projected_market_value(
    current_prices=current_quotes
)
pprint.pprint(portfolio_summary)

# Grab current MSFT price to use as initial trade price.
msft_price = trading_robot.grab_current_quotes()['MSFT']['quote']['lastPrice']

# Create a new Trade Object.
new_trade = trading_robot.create_trade(
    trade_id='long_msft',
    enter_or_exit='enter',
    long_or_short='long',
    order_type='lmt',
    price=msft_price
)

# Make it Good Till Cancel.
new_trade.good_till_cancel(cancel_time=datetime.now())

# Change the session
new_trade.modify_session(session='normal')

# Add an Order Leg.
new_trade.instrument(
    symbol='MSFT',
    quantity=2,
    asset_type='EQUITY'
)

# Add a Stop Loss Order with the Main Order (2% stop loss).
new_trade.add_stop_loss(
    stop_size=.02,
    percentage=True
)

# Print out the order.
pprint.pprint(new_trade.order)

# Grab historical prices, first define the start date and end date.
start_date = datetime.today()
end_date = start_date - timedelta(days=30)

# Grab the historical prices.
historical_prices = trading_robot.grab_historical_prices(
    start=end_date,
    end=start_date,
    bar_size=1,
    bar_type='minute'
)

# Convert data to a Data Frame.
stock_frame = trading_robot.create_stock_frame(
    data=historical_prices['aggregated']
)

# We can also add the stock frame to the Portfolio object.
trading_robot.portfolio.stock_frame = stock_frame

# Additionally the historical prices can be set as well.
trading_robot.portfolio.historical_prices = historical_prices

# Portfolio Variance
pprint.pprint(trading_robot.portfolio.portfolio_metrics())

# Create an indicator Object.
indicator_client = Indicators(price_data_frame=stock_frame)

# Add the RSI Indicator.
indicator_client.rsi(period=14)

# Add the 200 day simple moving average.
indicator_client.sma(period=200)

# Add the 200 day simple moving average.
indicator_client.sma(period=50)

# Add the 50 day exponentials moving average.
indicator_client.ema(period=50)

# Add a signal to check for.
# Buy when RSI is oversold (<= 30), sell when overbought (>= 70).
indicator_client.set_indicator_signal(
    indicator='rsi',
    buy=30.0,
    sell=70.0,
    condition_buy=operator.le,
    condition_sell=operator.ge
)

# Define a trading dictionary.
trades_dict = {
    'MSFT': {
        'buy': {
            'trade_func': trading_robot.trades['long_msft'],
            'trade_id': trading_robot.trades['long_msft'].trade_id
        },
        'sell': {
            'trade_func': trading_robot.trades['long_msft'],
            'trade_id': trading_robot.trades['long_msft'].trade_id
        }
    }
}

while True:

    # Grab the latest bar.
    latest_bars = trading_robot.get_latest_bar()

    # Add to the Stock Frame.
    stock_frame.add_rows(data=latest_bars)

    # Refresh the Indicators.
    indicator_client.refresh()

    print("="*50)
    print("Current StockFrame")
    print("-"*50)
    print(stock_frame.symbol_groups.tail())
    print("-"*50)
    print("")

    # Update trade price to current market price before checking signals.
    current_msft_price = trading_robot.grab_current_quotes()['MSFT']['quote']['lastPrice']
    trading_robot.trades['long_msft'].modify_price(
        new_price=current_msft_price,
        price_type='limit-price'
    )

    # Check for signals.
    signals = indicator_client.check_signals()

    # Execute Trades.
    order_responses = trading_robot.execute_signals(
        signals=signals,
        trades_to_execute=trades_dict
    )

    # Print any executed orders with color.
    for response in order_responses:
        instruction = response['request_body'].get('orderLegCollection', [{}])[0].get('instruction', '')
        if 'BUY' in instruction.upper():
            print("\033[92m" + "=" * 50)
            print("BUY ORDER EXECUTED")
            pprint.pprint(response)
            print("=" * 50 + "\033[0m")
        elif 'SELL' in instruction.upper():
            print("\033[91m" + "=" * 50)
            print("SELL ORDER EXECUTED")
            pprint.pprint(response)
            print("=" * 50 + "\033[0m")

    # Grab the last bar.
    last_bar_timestamp = trading_robot.stock_frame.frame.tail(
        n=1
    ).index.get_level_values(1)

    # Wait till the next bar.
    trading_robot.wait_till_next_bar(last_bar_timestamp=last_bar_timestamp)
