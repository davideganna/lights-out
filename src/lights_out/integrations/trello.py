"""Trello adapter: read cards from the board, comment on them, move them between lists."""

from __future__ import annotations

import httpx

from ..core.models import Comment, Ticket

API = "https://api.trello.com/1"


class TrelloError(RuntimeError):
    pass


class TrelloClient:
    def __init__(self, api_key: str, token: str, board_id: str, *, http: httpx.Client | None = None):
        self.board_id = board_id
        # Credentials go in the Authorization header so they never show up in logged URLs.
        self._http = http or httpx.Client(
            base_url=API,
            timeout=30,
            headers={"Authorization": f'OAuth oauth_consumer_key="{api_key}", oauth_token="{token}"'},
        )
        self._list_ids: dict[str, str] | None = None

    def _req(self, method: str, path: str, **kw):
        r = self._http.request(method, path, **kw)
        if r.status_code >= 400:
            raise TrelloError(f"Trello {method} {path} -> {r.status_code}: {r.text[:300]}")
        return r.json() if r.content else None

    # --- lists -----------------------------------------------------------
    def list_ids(self, refresh: bool = False) -> dict[str, str]:
        if self._list_ids is None or refresh:
            lists = self._req("GET", f"/boards/{self.board_id}/lists", params={"filter": "open"})
            self._list_ids = {lst["name"]: lst["id"] for lst in lists}
        return self._list_ids

    def list_id(self, name: str) -> str:
        ids = self.list_ids()
        if name not in ids:
            raise TrelloError(f"Trello list {name!r} not found on board (have: {sorted(ids)})")
        return ids[name]

    def ensure_lists(self, names: list[str]) -> list[str]:
        """Create any missing lists (appended at the right). Returns the names created."""
        existing = self.list_ids(refresh=True)
        created = []
        for name in names:
            if name not in existing:
                self._req("POST", "/lists", params={"name": name, "idBoard": self.board_id, "pos": "bottom"})
                created.append(name)
        if created:
            self.list_ids(refresh=True)
        return created

    # --- labels ----------------------------------------------------------
    def ensure_labels(self, names: list[str], color: str = "sky") -> list[str]:
        """Create any missing board labels (matched case-insensitively). Returns the names created."""
        existing = {(lbl.get("name") or "").lower() for lbl in self._req("GET", f"/boards/{self.board_id}/labels")}
        created = []
        for name in names:
            if name.lower() not in existing:
                self._req("POST", "/labels", params={"name": name, "color": color, "idBoard": self.board_id})
                created.append(name)
        return created

    # --- cards -----------------------------------------------------------
    def cards_in(self, list_name: str) -> list[Ticket]:
        """Cards in a list, in board order (top first)."""
        cards = self._req(
            "GET",
            f"/lists/{self.list_id(list_name)}/cards",
            params={"fields": "name,desc,idShort,shortUrl,labels,pos"},
        )
        cards.sort(key=lambda c: c["pos"])
        return [self._to_ticket(c, with_details=False) for c in cards]

    def get_ticket(self, card_id: str) -> Ticket:
        card = self._req("GET", f"/cards/{card_id}", params={"fields": "name,desc,idShort,shortUrl,labels"})
        return self._to_ticket(card, with_details=True)

    def _to_ticket(self, card: dict, *, with_details: bool) -> Ticket:
        t = Ticket(
            id=card["id"],
            short_id=str(card["idShort"]),
            title=card["name"],
            description=card.get("desc", ""),
            url=card["shortUrl"],
            labels=[lbl.get("name") or lbl.get("color", "") for lbl in card.get("labels", [])],
        )
        if with_details:
            t.checklists = self._checklists(card["id"])
            t.comments = self._comments(card["id"])
        return t

    def _checklists(self, card_id: str) -> list[str]:
        out = []
        for cl in self._req("GET", f"/cards/{card_id}/checklists"):
            out.append(f"{cl['name']}:")
            for item in sorted(cl["checkItems"], key=lambda i: i["pos"]):
                mark = "x" if item["state"] == "complete" else " "
                out.append(f"  - [{mark}] {item['name']}")
        return out

    def _comments(self, card_id: str) -> list[Comment]:
        actions = self._req("GET", f"/cards/{card_id}/actions", params={"filter": "commentCard", "limit": 100})
        comments = [
            Comment(
                author=a.get("memberCreator", {}).get("fullName", "unknown"),
                text=a["data"]["text"],
                date=a["date"][:16].replace("T", " "),
            )
            for a in actions
        ]
        return list(reversed(comments))  # API returns newest first

    def move(self, card_id: str, list_name: str, *, top: bool = True) -> None:
        self._req(
            "PUT", f"/cards/{card_id}", params={"idList": self.list_id(list_name), "pos": "top" if top else "bottom"}
        )

    def comment(self, card_id: str, text: str) -> None:
        # Trello caps comments at 16384 chars.
        self._req("POST", f"/cards/{card_id}/actions/comments", json={"text": text[:16000]})
