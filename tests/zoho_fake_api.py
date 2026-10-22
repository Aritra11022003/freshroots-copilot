"""In-memory fake of the few Zoho Inventory operations the copilot uses (for sync/push tests)."""
import itertools

from copilot.zoho_client import ZohoError


class FakeZoho:
    def __init__(self):
        self.contacts, self.items, self.pos = [], [], []
        self._ids = itertools.count(1000)
        self.posts, self.fail_po_create = [], 0

    def _id(self):
        return str(next(self._ids))

    def list_contacts(self, **f):
        return [c for c in self.contacts if all(c.get(k) == v for k, v in f.items())]

    def list_items(self, **f):
        return list(self.items)

    def list_purchase_orders(self, **f):
        return [{k: v for k, v in p.items() if k != "line_items"} for p in self.pos]

    def get_purchase_order(self, pid):
        return next(p for p in self.pos if p["purchaseorder_id"] == pid)

    def post(self, path, json, params=None):
        self.posts.append((path, json))
        if path == "contacts":
            c = {"contact_id": self._id(), **json}
            self.contacts.append(c)
            return {"code": 0, "contact": c}
        if path == "items":
            i = {"item_id": self._id(), **json}
            self.items.append(i)
            return {"code": 0, "item": i}
        if path == "purchaseorders":
            if self.fail_po_create:
                self.fail_po_create -= 1
                raise ZohoError("Zoho is busy (rate limit or server error); please retry shortly", 429,
                                "POST /purchaseorders")
            n = len(self.pos) + 1
            po = {"purchaseorder_id": self._id(), "purchaseorder_number": f"PO-{n:05d}", "status": "draft",
                  "received_status": "", **json,
                  "line_items": [dict(li, quantity_received=0) for li in json["line_items"]]}
            self.pos.append(po)
            return {"code": 0, "purchaseorder": po}
        raise AssertionError(f"unexpected POST {path}")
