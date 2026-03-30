from app.core.database import get_collection
import logging

logger = logging.getLogger(__name__)

async def execute_safe_exit_all(user_id: str, kotak_client):
    """
    CRITICAL ALGO RULE: 
    When 'Exit All' is triggered, the exit order for "BUY" positions 
    MUST be placed before "SELL" positions to prevent margin shortfall.
    """
    trades_col = get_collection("trades")
    
    # 1. User ke saare OPEN trades fetch karein
    open_trades_cursor = trades_col.find({"user_id": user_id, "status": "OPEN"})
    open_trades = await open_trades_cursor.to_list(length=1000)
    
    if not open_trades:
        return {"status": "success", "message": "No open positions to exit."}

    # 2. Trades ko Buy aur Sell mein alag karein
    buy_positions = [t for t in open_trades if t["side"] == "BUY"]
    sell_positions = [t for t in open_trades if t["side"] == "SELL"]

    logger.info(f"🚨 PANIC EXIT INITIATED for User: {user_id}")
    logger.info(f"-> Found {len(buy_positions)} BUYs and {len(sell_positions)} SELLs.")

    # 3. STEP A: EXIT ALL BUY POSITIONS FIRST
    for pos in buy_positions:
        try:
            # BUY ko exit karne ke liye SELL order lagana padta hai
            # Note: kotak_client aapka active NeoAPI instance hoga
            kotak_client.place_order(
                exchange_segment="nse_fo", # Ya jo bhi position ka segment ho
                product="NRML",
                price="0",
                order_type="MKT",
                quantity=str(pos["qty"]),
                validity="DAY",
                trading_symbol=pos["trade_symbol"],
                transaction_type="S", # Exit action
                amo="NO"
            )
            # DB mein update karein
            await trades_col.update_one({"_id": pos["_id"]}, {"$set": {"status": "CLOSED"}})
            logger.info(f"✅ Exited BUY position: {pos['trade_symbol']}")
        except Exception as e:
            logger.error(f"❌ Failed to exit BUY position {pos['trade_symbol']}: {str(e)}")

    # 4. STEP B: EXIT ALL SELL POSITIONS NEXT
    for pos in sell_positions:
        try:
            # SELL ko exit karne ke liye BUY order lagana padta hai
            kotak_client.place_order(
                exchange_segment="nse_fo",
                product="NRML",
                price="0",
                order_type="MKT",
                quantity=str(pos["qty"]),
                validity="DAY",
                trading_symbol=pos["trade_symbol"],
                transaction_type="B", # Exit action
                amo="NO"
            )
            await trades_col.update_one({"_id": pos["_id"]}, {"$set": {"status": "CLOSED"}})
            logger.info(f"✅ Exited SELL position: {pos['trade_symbol']}")
        except Exception as e:
            logger.error(f"❌ Failed to exit SELL position {pos['trade_symbol']}: {str(e)}")

    return {"status": "success", "message": "Safe Exit Sequence Completed (Buys exited before Sells)."}
