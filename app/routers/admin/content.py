"""Blog posts and legal pages with draft/published state."""
from __future__ import annotations

import re
import sqlite3

from fastapi import APIRouter, Depends, Form, HTTPException, Request

from ...db import all_rows, one, transaction
from ...deps import flash, get_db, ip, redirect, require_admin
from ...security import iso
from ...services import audit
from .common import arender, as_int

router = APIRouter()


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")[:100] or "post"


@router.get("/posts")
def posts(request: Request, conn: sqlite3.Connection = Depends(get_db), admin: dict = Depends(require_admin)):
    rows = [dict(r) for r in all_rows(conn, "SELECT id, slug, title, status, published_at, updated_at FROM posts ORDER BY COALESCE(published_at, created_at) DESC")]
    pages = [dict(r) for r in all_rows(conn, "SELECT id, slug, title, status, updated_at, length(body) AS body_len FROM pages ORDER BY slug")]
    return arender(request, "admin/posts.html", {"posts": rows, "pages": pages, "meta_title": "Content"}, conn, admin)


@router.get("/posts/new")
def post_new(request: Request, conn: sqlite3.Connection = Depends(get_db), admin: dict = Depends(require_admin)):
    return arender(request, "admin/post_form.html", {"post": None, "meta_title": "New post"}, conn, admin)


@router.get("/posts/{post_id}")
def post_edit(post_id: int, request: Request, conn: sqlite3.Connection = Depends(get_db), admin: dict = Depends(require_admin)):
    row = one(conn, "SELECT * FROM posts WHERE id = ?", (post_id,))
    if not row:
        raise HTTPException(404)
    return arender(request, "admin/post_form.html", {"post": dict(row), "meta_title": f"Edit: {row['title']}"}, conn, admin)


@router.post("/posts/save")
def post_save(request: Request, id: str = Form("0"), title: str = Form(""), slug: str = Form(""), excerpt: str = Form(""), body: str = Form(""), author: str = Form("Quick Drain"), status: str = Form("draft"), seo_title: str = Form(""), seo_description: str = Form(""), conn: sqlite3.Connection = Depends(get_db), admin: dict = Depends(require_admin)):
    pid = as_int(id)
    title = title.strip()[:200]
    if not title:
        flash(request, "A post needs a title.", "error")
        return redirect("/admin/posts")
    status = "published" if status == "published" else "draft"
    data = {"title": title, "slug": _slug(slug or title), "excerpt": excerpt.strip()[:500], "body": body.strip()[:60000], "author": author.strip()[:80] or "Quick Drain", "status": status, "seo_title": seo_title.strip()[:150], "seo_description": seo_description.strip()[:300], "updated_at": iso()}
    with transaction(conn):
        before = one(conn, "SELECT * FROM posts WHERE id = ?", (pid,)) if pid else None
        if before:
            if status == "published" and not before["published_at"]:
                data["published_at"] = iso()
            if one(conn, "SELECT 1 FROM posts WHERE slug = ? AND id != ?", (data["slug"], pid)):
                data["slug"] = f"{data['slug']}-{pid}"
            sets = ", ".join(f"{k} = ?" for k in data)
            conn.execute(f"UPDATE posts SET {sets} WHERE id = ?", (*data.values(), pid))
        else:
            if one(conn, "SELECT 1 FROM posts WHERE slug = ?", (data["slug"],)):
                data["slug"] = f"{data['slug']}-2"
            if status == "published":
                data["published_at"] = iso()
            cur = conn.execute(f"INSERT INTO posts ({', '.join(data)}) VALUES ({', '.join('?' for _ in data)})", tuple(data.values()))
            pid = int(cur.lastrowid)
        audit.log(conn, "post.save", actor_type="admin", actor_id=admin["id"], actor_name=admin["username"], target_type="post", target_id=pid, before={"status": before["status"], "title": before["title"]} if before else None, after={"status": status, "title": title}, ip=ip(request))
    flash(request, "Post saved.")
    return redirect(f"/admin/posts/{pid}")


@router.post("/posts/{post_id}/delete")
def post_delete(post_id: int, request: Request, conn: sqlite3.Connection = Depends(get_db), admin: dict = Depends(require_admin)):
    row = one(conn, "SELECT title FROM posts WHERE id = ?", (post_id,))
    if row:
        with transaction(conn):
            conn.execute("DELETE FROM posts WHERE id = ?", (post_id,))
            audit.log(conn, "post.delete", actor_type="admin", actor_id=admin["id"], actor_name=admin["username"], target_type="post", target_id=post_id, before={"title": row["title"]}, ip=ip(request))
    return redirect("/admin/posts")


@router.get("/pages/{page_id}")
def page_edit(page_id: int, request: Request, conn: sqlite3.Connection = Depends(get_db), admin: dict = Depends(require_admin)):
    row = one(conn, "SELECT * FROM pages WHERE id = ?", (page_id,))
    if not row:
        raise HTTPException(404)
    return arender(request, "admin/page_form.html", {"page": dict(row), "meta_title": f"Edit: {row['title']}"}, conn, admin)


@router.post("/pages/{page_id}")
def page_save(page_id: int, request: Request, title: str = Form(""), body: str = Form(""), status: str = Form("published"), conn: sqlite3.Connection = Depends(get_db), admin: dict = Depends(require_admin)):
    row = one(conn, "SELECT * FROM pages WHERE id = ?", (page_id,))
    if not row:
        raise HTTPException(404)
    with transaction(conn):
        conn.execute("UPDATE pages SET title = ?, body = ?, status = ?, updated_at = ? WHERE id = ?", (title.strip()[:150] or row["title"], body.strip()[:60000], "published" if status == "published" else "draft", iso(), page_id))
        audit.log(conn, "page.save", actor_type="admin", actor_id=admin["id"], actor_name=admin["username"], target_type="page", target_id=page_id, before={"len": len(row["body"])}, after={"len": len(body)}, ip=ip(request))
    flash(request, "Page saved. An empty body shows the built-in default text.")
    return redirect(f"/admin/pages/{page_id}")
