"""Flask routes: dashboard and healthcheck."""

import logging
from datetime import date, timedelta

import requests as http_requests
from flask import Blueprint, jsonify, render_template, request
from sqlalchemy import func
from sqlalchemy.orm import Session, subqueryload

from app.models import Crawl, Town

logger = logging.getLogger(__name__)

bp = Blueprint("main", __name__)

ROWS_PER_PAGE = 20


@bp.route("/")
def dashboard():
    """Main dashboard page."""
    # Time filters
    period = request.args.get("period", "month")
    date_from = request.args.get("from")
    date_to = request.args.get("to")
    page = request.args.get("page", 1, type=int)
    town_id = request.args.get("town", type=int)

    today = date.today()

    if date_from and date_to:
        period = "custom"
        try:
            start = date.fromisoformat(date_from)
            end = date.fromisoformat(date_to)
        except ValueError:
            start = today - timedelta(days=30)
            end = today
    elif period == "year":
        start = today - timedelta(days=365)
        end = today
    else:
        start = today - timedelta(days=30)
        end = today

    from app.models import get_session_factory

    session: Session = get_session_factory()()
    try:
        towns = session.query(Town).filter_by(active=True).order_by(Town.name).all()
        all_towns = session.query(Town).order_by(Town.name).all()

        if not town_id and towns:
            town_id = towns[0].id

        selected_town_obj = next((t for t in towns if t.id == town_id), None)
        selected_town_name = selected_town_obj.name if selected_town_obj else "Villes"

        query = (
            session.query(Crawl)
            .options(subqueryload(Crawl.notifications))
            .filter(
                Crawl.status == "success",
                Crawl.crawl_date >= start,
                Crawl.crawl_date <= end,
            )
            .order_by(Crawl.crawl_date.desc())
        )

        if town_id:
            query = query.filter(Crawl.town_id == town_id)

        total = query.count()
        total_pages = max(1, (total + ROWS_PER_PAGE - 1) // ROWS_PER_PAGE)
        page = max(1, min(page, total_pages))

        crawls = query.offset((page - 1) * ROWS_PER_PAGE).limit(ROWS_PER_PAGE).all()

        # Chart.js data (all entries for the period, chronological order)
        chart_query = (
            session.query(
                Crawl.crawl_date,
                Crawl.unit_price,
                Crawl.unit_price_with_discount,
            )
            .filter(
                Crawl.status == "success",
                Crawl.crawl_date >= start,
                Crawl.crawl_date <= end,
            )
        )

        if town_id:
            chart_query = chart_query.filter(Crawl.town_id == town_id)

        chart_data = chart_query.order_by(Crawl.crawl_date.asc()).all()

        chart_labels = [row.crawl_date.isoformat() for row in chart_data]
        chart_prices = [float(row.unit_price) for row in chart_data]
        chart_discounts = [
            float(row.unit_price_with_discount) if row.unit_price_with_discount else None
            for row in chart_data
        ]

        return render_template(
            "dashboard.html",
            crawls=crawls,
            page=page,
            total_pages=total_pages,
            period=period,
            date_from=start.isoformat(),
            date_to=end.isoformat(),
            chart_labels=chart_labels,
            chart_prices=chart_prices,
            chart_discounts=chart_discounts,
            towns=towns,
            all_towns=all_towns,
            selected_town=town_id,
            selected_town_name=selected_town_name,
        )
    finally:
        session.close()


@bp.route("/health")
def health():
    """Healthcheck endpoint for Portainer."""
    from app.models import get_session_factory

    try:
        session: Session = get_session_factory()()
        try:
            last_crawl = (
                session.query(func.max(Crawl.crawl_date))
                .filter(Crawl.status == "success")
                .scalar()
            )
            records_count = session.query(Crawl).filter(Crawl.status == "success").count()

            return jsonify(
                {
                    "status": "healthy",
                    "last_crawl": last_crawl.isoformat() if last_crawl else None,
                    "records_count": records_count,
                }
            )
        finally:
            session.close()
    except Exception as e:
        logger.error("Healthcheck failed: %s", e)
        return jsonify({"status": "unhealthy", "error": str(e)}), 503


@bp.route("/api/towns/search")
def towns_search():
    """Search proxy to the TotalEnergies API (avoids CORS)."""
    q = request.args.get("query", "").strip()
    if len(q) < 3:
        return jsonify([])
    try:
        resp = http_requests.get(
            "https://api.proxi-totalenergies.fr/towns",
            params={"query": q},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        results = [
            {"code": str(t["id"]), "name": t["name"]}
            for t in data
        ]
        return jsonify(results)
    except Exception as e:
        logger.error("Town search failed: %s", e)
        return jsonify([])


@bp.route("/api/towns", methods=["POST"])
def towns_create():
    """Add a town."""
    from app.models import get_session_factory

    body = request.get_json(force=True)
    code = str(body.get("code", "")).strip()
    name = str(body.get("name", "")).strip()
    if not code or not name:
        return jsonify({"error": "code and name required"}), 400

    session: Session = get_session_factory()()
    try:
        existing = session.query(Town).filter_by(code=code).first()
        if existing:
            return jsonify({"error": "town already exists"}), 409
        town = Town(code=code, name=name, active=True)
        session.add(town)
        session.commit()
        town_data = {"id": town.id, "code": town.code, "name": town.name, "active": town.active}
        # Run an immediate first crawl for the new town
        try:
            from app.crawler import crawl_town
            from app.alerts import analyze_and_notify

            crawl = crawl_town(session, town)
            if crawl and crawl.status == "success":
                analyze_and_notify(session, crawl)
        except Exception:
            logger.exception("Error during initial crawl for %s", town.name)
        return jsonify(town_data), 201
    finally:
        session.close()


@bp.route("/api/towns/<int:town_id>", methods=["PATCH"])
def towns_update(town_id):
    """Enable/disable a town."""
    from app.models import get_session_factory

    body = request.get_json(force=True)
    session: Session = get_session_factory()()
    try:
        town = session.query(Town).get(town_id)
        if not town:
            return jsonify({"error": "not found"}), 404
        if "active" in body:
            town.active = bool(body["active"])
        session.commit()
        return jsonify({"id": town.id, "name": town.name, "active": town.active})
    finally:
        session.close()


@bp.route("/api/towns/<int:town_id>", methods=["DELETE"])
def towns_delete(town_id):
    """Delete a town and its associated crawls."""
    from app.models import get_session_factory
    from app.models import Notification

    session: Session = get_session_factory()()
    try:
        town = session.query(Town).get(town_id)
        if not town:
            return jsonify({"error": "not found"}), 404
        # Delete notifications from associated crawls
        crawl_ids = [c.id for c in session.query(Crawl.id).filter_by(town_id=town_id).all()]
        if crawl_ids:
            session.query(Notification).filter(Notification.crawl_id.in_(crawl_ids)).delete(synchronize_session=False)
        # Delete crawls then the town
        session.query(Crawl).filter_by(town_id=town_id).delete(synchronize_session=False)
        session.delete(town)
        session.commit()
        return jsonify({"ok": True})
    finally:
        session.close()
