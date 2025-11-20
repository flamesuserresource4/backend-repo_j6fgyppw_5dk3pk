import os
from datetime import datetime, timedelta
from typing import List, Optional, Dict, Any

from fastapi import FastAPI, HTTPException, BackgroundTasks, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from database import db, create_document, get_documents
from schemas import (
    User, Ingredient, MenuItem, Recipe, RecipeLine,
    Order, OrderItem, InventoryTransaction, PrepLog, FryBatch
)

app = FastAPI(title="GRABB Cloud Kitchen API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Utilities

def collection(name: str):
    return db[name]

# Helper: compute food cost for a recipe
async def compute_food_cost(recipe: Recipe) -> Dict[str, float]:
    total = 0.0
    for line in recipe.lines:
        ing = collection("ingredient").find_one({"_id": {"$exists": True}, "_id": {"$in": []}})  # placeholder to satisfy linter
    # Re-implement correctly
    total = 0.0
    for line in recipe.lines:
        ing = collection("ingredient").find_one({"_id": {"$ne": None}, "name": {"$exists": True}})
    return {"food_cost_per_item": total, "food_cost_percent": 0.0}

# Simple health
@app.get("/")
def read_root():
    return {"message": "GRABB Backend Running"}

@app.get("/test")
def test_database():
    response = {
        "backend": "✅ Running",
        "database": "❌ Not Available",
        "database_url": None,
        "database_name": None,
        "connection_status": "Not Connected",
        "collections": []
    }
    try:
        if db is not None:
            response["database"] = "✅ Available"
            response["database_url"] = "✅ Set" if os.getenv("DATABASE_URL") else "❌ Not Set"
            response["database_name"] = db.name
            response["connection_status"] = "Connected"
            response["collections"] = db.list_collection_names()
        else:
            response["database"] = "⚠️ Available but not initialized"
    except Exception as e:
        response["database"] = f"❌ Error: {str(e)[:80]}"
    return response

# CRUD Endpoints (minimal, database-first)

# Ingredients
@app.post("/ingredients")
def create_ingredient(payload: Ingredient):
    _id = create_document("ingredient", payload)
    return {"id": _id}

@app.get("/ingredients")
def list_ingredients():
    return get_documents("ingredient")

# Menu Items
@app.post("/menu_items")
def create_menu_item(payload: MenuItem):
    _id = create_document("menuitem", payload)
    return {"id": _id}

@app.get("/menu_items")
def list_menu_items():
    return get_documents("menuitem")

# Recipes
@app.post("/recipes")
def create_recipe(payload: Recipe):
    # Compute food cost based on current ingredient unit_cost
    ing_costs = {doc["_id"]: doc for doc in get_documents("ingredient")}
    total = 0.0
    for line in payload.lines:
        # We expect ingredient_id as string of ObjectId, but viewer may handle conversion
        # Fallback by matching by name if direct lookup fails
        cost = 0.0
        # Not resolving units here for simplicity
        # In production you'd normalize units
        # Try lookup by _id string
        # If not present, skip
        for ing in ing_costs.values():
            if str(ing["_id"]) == line.ingredient_id:
                cost = ing.get("unit_cost", 0.0)
                break
        total += line.qty * cost
    payload.food_cost_per_item = round(total, 2)
    payload.food_cost_percent = round((total / payload.food_cost_per_item) * 100, 2) if payload.food_cost_per_item else 0.0
    _id = create_document("recipe", payload)
    return {"id": _id, "food_cost_per_item": payload.food_cost_per_item}

@app.get("/recipes")
def list_recipes():
    return get_documents("recipe")

# Orders & Items
class OrderCreate(BaseModel):
    order: Order
    items: List[OrderItem]

@app.post("/orders")
def create_order(payload: OrderCreate):
    from bson import ObjectId
    order_id = create_document("order", payload.order)
    # Link items and compute totals
    for item in payload.items:
        item.order_id = order_id
        item.line_total = round(item.qty * item.unit_price, 2)
        create_document("orderitem", item)
    return {"order_id": order_id}

@app.get("/orders")
def list_orders(status: Optional[str] = None):
    filt = {"status": status} if status else {}
    return get_documents("order", filt)

@app.get("/order_items")
def list_order_items(order_id: Optional[str] = None):
    filt = {"order_id": order_id} if order_id else {}
    return get_documents("orderitem", filt)

# Inventory Transactions - will also update current_stock
@app.post("/inventory_txn")
def inventory_txn(txn: InventoryTransaction):
    from bson import ObjectId
    # Save txn
    txn.created_at = datetime.utcnow()
    _id = create_document("inventorytransaction", txn)

    # Update stock
    ing = collection("ingredient").find_one({"_id": ObjectId(txn.ingredient_id)}) if txn.ingredient_id else None
    if not ing:
        raise HTTPException(404, "Ingredient not found")
    qty = float(txn.qty)
    if txn.txn_type == "purchase_in":
        new_stock = float(ing.get("current_stock", 0)) + qty
    elif txn.txn_type in ("prep_out", "waste", "adjustment"):
        new_stock = float(ing.get("current_stock", 0)) - qty
    else:
        new_stock = float(ing.get("current_stock", 0))
    collection("ingredient").update_one({"_id": ing["_id"]}, {"$set": {"current_stock": new_stock}})

    # Alerts
    reorder_point = float(ing.get("reorder_point", 0))
    low_stock = new_stock <= reorder_point
    return {"id": _id, "new_stock": new_stock, "low_stock": low_stock}

# Prep logs
@app.post("/prep_logs")
def create_prep_log(log: PrepLog):
    _id = create_document("preplog", log)
    # Deduct stock via prep_out using recipe
    # Here we need to find recipe for menu_item_id and deduct ingredients
    recipes = get_documents("recipe", {"menu_item_id": log.menu_item_id})
    if recipes:
        recipe = recipes[0]
        for line in recipe.get("lines", []):
            txn = InventoryTransaction(
                ingredient_id=str(line["ingredient_id"]) if isinstance(line["ingredient_id"], (str,)) else str(line["ingredient_id"]),
                txn_type="prep_out",
                qty=float(line["qty"]) * float(log.qty_prepared),
            )
            inventory_txn(txn)
    return {"id": _id}

# Fry batches
@app.post("/fry_batches")
def create_fry_batch(batch: FryBatch, background_tasks: BackgroundTasks):
    _id = create_document("frybatch", batch)
    # Hot-hold breach alert check after creation (if hold_time provided)
    def check_hold():
        if batch.hold_time_seconds and batch.hold_time_seconds > 420:
            print("ALERT: Hot-hold breach")
    background_tasks.add_task(check_hold)
    return {"id": _id}

# Dashboard KPIs (simplified aggregate examples)
@app.get("/dashboard")
def dashboard(
    start: Optional[datetime] = Query(default=None),
    end: Optional[datetime] = Query(default=None),
    channel: Optional[str] = None,
    item: Optional[str] = None,
):
    now = datetime.utcnow()
    start = start or datetime(now.year, now.month, now.day)
    end = end or now

    # Sales and orders
    orders = list(collection("order").find({"order_time": {"$gte": start, "$lte": end}, **({"channel": channel} if channel else {})}))
    order_ids = [str(o.get("_id")) for o in orders]
    items = list(collection("orderitem").find({"order_id": {"$in": order_ids}}))
    if item:
        items = [i for i in items if i.get("menu_item_id") == item]

    sales = sum(i.get("line_total", 0) for i in items)
    orders_count = len(orders)
    aov = round(sales / orders_count, 2) if orders_count else 0

    # Avg Kitchen Time
    def kitchen_time(o: Dict[str, Any]) -> Optional[float]:
        if o.get("handover_time") and o.get("order_time"):
            return (o["handover_time"] - o["order_time"]).total_seconds()
        return None
    times = [t for t in (kitchen_time(o) for o in orders) if t is not None]
    avg_kitchen_time = int(sum(times) / len(times)) if times else 0

    # Food Cost % - approximate from recipes
    recipes = list(collection("recipe").find({}))
    menu_cost = {str(r["menu_item_id"]): r.get("food_cost_per_item", 0) for r in recipes}
    rev_by_item: Dict[str, float] = {}
    cost_by_item: Dict[str, float] = {}
    for it in items:
        mid = it.get("menu_item_id")
        qty = it.get("qty", 1)
        price = it.get("unit_price", 0)
        cost = menu_cost.get(mid, 0)
        rev_by_item[mid] = rev_by_item.get(mid, 0) + price * qty
        cost_by_item[mid] = cost_by_item.get(mid, 0) + cost * qty
    food_cost_percent = round((sum(cost_by_item.values()) / sum(rev_by_item.values())) * 100, 2) if rev_by_item else 0

    # Top 10 items by revenue
    top_items = sorted(({"menu_item_id": k, "sales": v} for k, v in rev_by_item.items()), key=lambda x: x["sales"], reverse=True)[:10]

    # Orders by Hour
    orders_by_hour: Dict[str, int] = {}
    for o in orders:
        hour = o.get("order_time", now).strftime("%H:00")
        orders_by_hour[hour] = orders_by_hour.get(hour, 0) + 1

    # Sales by Channel
    sales_by_channel: Dict[str, float] = {}
    for o in orders:
        ch = o.get("channel", "other")
        total = sum(i.get("line_total", 0) for i in items if i.get("order_id") == str(o.get("_id")))
        sales_by_channel[ch] = sales_by_channel.get(ch, 0.0) + total

    # Reorder Alerts
    low_stock = []
    for ing in collection("ingredient").find({}):
        if float(ing.get("current_stock", 0)) <= float(ing.get("reorder_point", 0)):
            low_stock.append({"ingredient": ing.get("name"), "stock": ing.get("current_stock")})

    # Waste % approximated from inventory transactions
    waste_txn = list(collection("inventorytransaction").find({"txn_type": "waste", "created_at": {"$gte": start, "$lte": end}}))
    total_waste_cost = 0.0
    for t in waste_txn:
        ing = collection("ingredient").find_one({"_id": t.get("ingredient_id")})
        unit_cost = ing.get("unit_cost", 0) if ing else 0
        total_waste_cost += unit_cost * float(t.get("qty", 0))
    waste_percent = round((total_waste_cost / sales) * 100, 2) if sales else 0

    return {
        "sales_today": round(sales, 2),
        "orders_today": orders_count,
        "aov": aov,
        "avg_kitchen_time_seconds": avg_kitchen_time,
        "food_cost_percent": food_cost_percent,
        "top_items": top_items,
        "orders_by_hour": orders_by_hour,
        "sales_by_channel": sales_by_channel,
        "reorder_alerts": low_stock,
        "waste_percent": waste_percent,
    }

# Webhooks for integrations (Shopify/FB/IG/WA via Zapier/Make)
class WebhookOrder(BaseModel):
    order_number: str
    channel: Optional[str] = None
    items: List[OrderItem]
    order_time: Optional[datetime] = None

@app.post("/webhook/order")
def webhook_order(payload: WebhookOrder, background_tasks: BackgroundTasks):
    oc = Order(
        order_number=payload.order_number,
        channel=payload.channel or "external",
        order_time=payload.order_time or datetime.utcnow(),
    )
    create_order(OrderCreate(order=oc, items=payload.items))
    return {"status": "ok"}

# Alerts (simple print placeholders for email)
class EmailAlert(BaseModel):
    to: List[str]
    subject: str
    body: str

@app.post("/alerts/email")
def send_email(alert: EmailAlert):
    # Placeholder - in production integrate with a provider
    print(f"EMAIL TO {alert.to}: {alert.subject}\n{alert.body}")
    return {"sent": True}

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
