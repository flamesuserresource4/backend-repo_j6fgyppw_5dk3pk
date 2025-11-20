"""
Database Schemas for GRABB Cloud Kitchen

Each Pydantic model represents a MongoDB collection. Collection name is the lowercase
of the class name (e.g., Ingredient -> "ingredient").

These schemas are used for request/response validation and to help the DB viewer.
"""
from typing import List, Optional, Literal
from pydantic import BaseModel, Field
from datetime import datetime

# Users & Roles
class User(BaseModel):
    name: str
    email: str
    role: Literal["admin", "manager", "kitchen", "accountant"] = Field("kitchen")
    is_active: bool = True

# Ingredients
class Ingredient(BaseModel):
    name: str
    unit: Literal["kg", "g", "litre", "ml", "pcs"]
    unit_cost: float = Field(ge=0)
    current_stock: float = 0.0
    lead_time_days: int = 0
    safety_stock: float = 0.0
    reorder_point: float = 0.0

# Menu Items
class MenuItem(BaseModel):
    name: str
    sku: Optional[str] = None
    price: float = Field(ge=0)
    is_active: bool = True

# Recipe lines and Recipe per menu item
class RecipeLine(BaseModel):
    ingredient_id: str
    qty: float = Field(ge=0)
    unit: Literal["kg", "g", "litre", "ml", "pcs"]

class Recipe(BaseModel):
    menu_item_id: str
    lines: List[RecipeLine] = []
    food_cost_per_item: float = 0.0
    food_cost_percent: float = 0.0

# Orders & Items
OrderStatus = Literal["placed", "prepping", "ready", "packed", "handed_over"]

class OrderItem(BaseModel):
    order_id: Optional[str] = None
    menu_item_id: str
    qty: int = Field(ge=1)
    unit_price: float = Field(ge=0)
    line_total: float = 0.0

class Order(BaseModel):
    order_number: str
    channel: Optional[str] = Field(default="pos", description="pos, shopify, fb, ig, whatsapp, swiggy, zomato, etc.")
    status: OrderStatus = "placed"
    order_time: datetime = Field(default_factory=datetime.utcnow)
    prep_start: Optional[datetime] = None
    prep_end: Optional[datetime] = None
    pack_time: Optional[datetime] = None
    handover_time: Optional[datetime] = None
    sla_target_seconds: int = 900
    notes: Optional[str] = None

# Inventory Transactions
class InventoryTransaction(BaseModel):
    ingredient_id: str
    txn_type: Literal["purchase_in", "prep_out", "waste", "adjustment"]
    qty: float
    unit_cost: Optional[float] = None
    reason: Optional[str] = None
    created_at: Optional[datetime] = None

# Prep Logs
class PrepLog(BaseModel):
    menu_item_id: str
    date: datetime = Field(default_factory=datetime.utcnow)
    qty_prepared: int = 0
    qty_used: int = 0
    qty_waste: int = 0

# Fry Batches
class FryBatch(BaseModel):
    start_time: datetime = Field(default_factory=datetime.utcnow)
    end_time: Optional[datetime] = None
    weight_g: Optional[float] = None
    hold_time_seconds: Optional[int] = None

"""
Note:
- food_cost_per_item = sum(recipe_qty × ingredient.unit_cost in same unit base)
- reorder_point = (avg_daily_usage × lead_time_days) + safety_stock
- Hot-hold breach when hold_time_seconds > 420
"""
