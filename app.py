import os
import uuid
from datetime import datetime, timedelta
from functools import wraps

from flask import (
    Flask, render_template, request, redirect, url_for, session, flash
)
from pymongo import MongoClient, ASCENDING
from werkzeug.security import generate_password_hash, check_password_hash
from bson.objectid import ObjectId
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "cambia_esto")

client = MongoClient(os.environ.get("MONGO_URI"))
db = client.get_database("biblioteca_db")

MINUTOS_EXPIRACION = 15


# ------------------------------------------------------------------
# Utilidades
# ------------------------------------------------------------------
def ahora():
    return datetime.utcnow()


def init_db():
    db.usuarios.create_index([("email", ASCENDING)], unique=True)
    db.usuarios.create_index([("matricula", ASCENDING)], unique=True)
    db.libros.create_index([("titulo", ASCENDING)])
    db.apartados.create_index([("codigo_confirmacion", ASCENDING)])
    db.apartados.create_index([("usuario_id", ASCENDING)])
    db.prestamos.create_index([("usuario_id", ASCENDING)])


def login_required(f):
    @wraps(f)
    def wrapper(*a, **kw):
        if not session.get("usuario_id"):
            flash("Inicia sesión para continuar.", "warning")
            return redirect(url_for("login"))
        return f(*a, **kw)
    return wrapper


def admin_required(f):
    @wraps(f)
    def wrapper(*a, **kw):
        if session.get("rol") != "admin":
            flash("Acceso solo para administradores.", "danger")
            return redirect(url_for("index"))
        return f(*a, **kw)
    return wrapper


def liberar_expirados():
    limite = ahora() - timedelta(minutes=MINUTOS_EXPIRACION)
    expirados = db.apartados.find(
        {"estado": "pendiente", "creado_en": {"$lt": limite}}
    )
    for ap in expirados:
        db.apartados.update_one(
            {"_id": ap["_id"]}, {"$set": {"estado": "expirado"}}
        )
        db.libros.update_one(
            {"_id": ap["libro_id"]}, {"$inc": {"ejemplares_disponibles": 1}}
        )


# ------------------------------------------------------------------
# Autenticación
# ------------------------------------------------------------------
@app.route("/registro", methods=["GET", "POST"])
def registro():
    if request.method == "POST":
        email = request.form["email"].strip().lower()
        if db.usuarios.find_one({"email": email}):
            flash("Ese correo ya está registrado.", "danger")
            return redirect(url_for("registro"))

        matricula = "BIB-" + uuid.uuid4().hex[:8].upper()
        usuario = {
            "nombre": request.form["nombre"].strip(),
            "email": email,
            "password_hash": generate_password_hash(request.form["password"]),
            "rol": "lector",
            "matricula": matricula,
            "carrera": request.form.get("carrera", "").strip(),
            "creado_en": ahora(),
        }
        db.usuarios.insert_one(usuario)
        flash("Registro exitoso. Ya puedes iniciar sesión.", "success")
        return redirect(url_for("login"))
    return render_template("registro.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form["email"].strip().lower()
        password = request.form["password"]
        usuario = db.usuarios.find_one({"email": email})
        if usuario and check_password_hash(usuario["password_hash"], password):
            session["usuario_id"] = str(usuario["_id"])
            session["rol"] = usuario["rol"]
            session["nombre"] = usuario["nombre"]
            flash(f"Bienvenido, {usuario['nombre']}.", "success")
            return redirect(url_for("index"))
        flash("Correo o contraseña incorrectos.", "danger")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    flash("Sesión cerrada.", "info")
    return redirect(url_for("index"))


# ------------------------------------------------------------------
# Catálogo público
# ------------------------------------------------------------------
@app.route("/")
def index():
    liberar_expirados()
    libros = list(
        db.libros.find(
            {"activo": True, "ejemplares_disponibles": {"$gt": 0}}
        ).sort("titulo", ASCENDING)
    )
    return render_template("index.html", libros=libros)


@app.route("/libro/<libro_id>")
def libro_detalle(libro_id):
    libro = db.libros.find_one({"_id": ObjectId(libro_id), "activo": True})
    if not libro:
        flash("Libro no encontrado.", "danger")
        return redirect(url_for("index"))
    return render_template("libro_detalle.html", libro=libro)


# ------------------------------------------------------------------
# Apartados
# ------------------------------------------------------------------
@app.route("/apartar/<libro_id>", methods=["POST"])
@login_required
def apartar(libro_id):
    liberar_expirados()
    libro = db.libros.find_one({"_id": ObjectId(libro_id), "activo": True})
    if not libro:
        flash("Libro no encontrado.", "danger")
        return redirect(url_for("index"))

    actualizado = db.libros.find_one_and_update(
        {"_id": libro["_id"], "ejemplares_disponibles": {"$gt": 0}},
        {"$inc": {"ejemplares_disponibles": -1}},
        return_document=True,
    )
    if not actualizado:
        flash("No hay ejemplares disponibles.", "warning")
        return redirect(url_for("index"))

    codigo = uuid.uuid4().hex[:6].upper()
    db.apartados.insert_one({
        "usuario_id": ObjectId(session["usuario_id"]),
        "libro_id": libro["_id"],
        "libro_titulo": libro["titulo"],
        "estado": "pendiente",
        "codigo_confirmacion": codigo,
        "creado_en": ahora(),
        "confirmado_en": None,
    })
    return redirect(url_for("confirmar", codigo=codigo))


@app.route("/confirmar/<codigo>")
@login_required
def confirmar(codigo):
    ap = db.apartados.find_one({
        "codigo_confirmacion": codigo,
        "usuario_id": ObjectId(session["usuario_id"]),
    })
    if not ap:
        flash("Código inválido.", "danger")
        return redirect(url_for("mis_apartados"))

    restante = MINUTOS_EXPIRACION - int(
        (ahora() - ap["creado_en"]).total_seconds() // 60
    )
    return render_template("confirmar.html", apartado=ap, restante=max(restante, 0))


@app.route("/confirmar/<codigo>/ok", methods=["POST"])
@login_required
def confirmar_ok(codigo):
    ap = db.apartados.find_one({
        "codigo_confirmacion": codigo,
        "usuario_id": ObjectId(session["usuario_id"]),
    })
    if not ap or ap["estado"] != "pendiente":
        flash("El apartado ya no está pendiente.", "danger")
        return redirect(url_for("mis_apartados"))

    limite = ahora() - timedelta(minutes=MINUTOS_EXPIRACION)
    if ap["creado_en"] < limite:
        liberar_expirados()
        flash("El código expiró. Vuelve a apartar el libro.", "warning")
        return redirect(url_for("index"))

    db.apartados.update_one(
        {"_id": ap["_id"]},
        {"$set": {"estado": "confirmado", "confirmado_en": ahora()}},
    )
    flash("Apartado confirmado.", "success")
    return redirect(url_for("mis_apartados"))


@app.route("/cancelar/<apartado_id>", methods=["POST"])
@login_required
def cancelar(apartado_id):
    ap = db.apartados.find_one({
        "_id": ObjectId(apartado_id),
        "usuario_id": ObjectId(session["usuario_id"]),
    })
    if ap and ap["estado"] in ("pendiente", "confirmado"):
        db.apartados.update_one(
            {"_id": ap["_id"]}, {"$set": {"estado": "cancelado"}}
        )
        db.libros.update_one(
            {"_id": ap["libro_id"]}, {"$inc": {"ejemplares_disponibles": 1}}
        )
        flash("Apartado cancelado.", "info")
    return redirect(url_for("mis_apartados"))


@app.route("/mis-apartados")
@login_required
def mis_apartados():
    liberar_expirados()
    apartados = list(
        db.apartados.find(
            {"usuario_id": ObjectId(session["usuario_id"])}
        ).sort("creado_en", -1)
    )
    return render_template("mis_apartados.html", apartados=apartados)


# ------------------------------------------------------------------
# Perfil y credencial
# ------------------------------------------------------------------
@app.route("/perfil")
@login_required
def perfil():
    usuario = db.usuarios.find_one({"_id": ObjectId(session["usuario_id"])})
    qr_data = f"BIB|{usuario['matricula']}|{usuario['email']}"
    return render_template("perfil.html", usuario=usuario, qr_data=qr_data)


# ------------------------------------------------------------------
# Panel de administración
# ------------------------------------------------------------------
@app.route("/admin")
@admin_required
def admin_panel():
    liberar_expirados()
    libros = list(db.libros.find().sort("titulo", ASCENDING))
    apartados = list(db.apartados.find().sort("creado_en", -1).limit(50))
    prestamos = list(db.prestamos.find().sort("fecha_prestamo", -1).limit(50))
    return render_template(
        "admin.html", libros=libros, apartados=apartados, prestamos=prestamos
    )


@app.route("/admin/libro/nuevo", methods=["POST"])
@admin_required
def admin_libro_nuevo():
    total = int(request.form["ejemplares_total"])
    db.libros.insert_one({
        "titulo": request.form["titulo"].strip(),
        "autor": request.form["autor"].strip(),
        "isbn": request.form.get("isbn", "").strip(),
        "categoria": request.form.get("categoria", "").strip(),
        "portada_url": request.form.get("portada_url", "").strip(),
        "ejemplares_total": total,
        "ejemplares_disponibles": total,
        "activo": True,
        "creado_en": ahora(),
    })
    flash("Libro agregado.", "success")
    return redirect(url_for("admin_panel"))


@app.route("/admin/libro/<libro_id>/editar", methods=["POST"])
@admin_required
def admin_libro_editar(libro_id):
    db.libros.update_one(
        {"_id": ObjectId(libro_id)},
        {"$set": {
            "titulo": request.form["titulo"].strip(),
            "autor": request.form["autor"].strip(),
            "isbn": request.form.get("isbn", "").strip(),
            "categoria": request.form.get("categoria", "").strip(),
            "portada_url": request.form.get("portada_url", "").strip(),
        }},
    )
    flash("Libro actualizado.", "success")
    return redirect(url_for("admin_panel"))


@app.route("/admin/libro/<libro_id>/estado", methods=["POST"])
@admin_required
def admin_libro_estado(libro_id):
    libro = db.libros.find_one({"_id": ObjectId(libro_id)})
    if libro:
        db.libros.update_one(
            {"_id": libro["_id"]}, {"$set": {"activo": not libro["activo"]}}
        )
    return redirect(url_for("admin_panel"))


@app.route("/admin/apartado/<apartado_id>/<estado>", methods=["POST"])
@admin_required
def admin_apartado_estado(apartado_id, estado):
    if estado not in ("confirmado", "cancelado", "expirado"):
        return redirect(url_for("admin_panel"))
    ap = db.apartados.find_one({"_id": ObjectId(apartado_id)})
    if ap and ap["estado"] == "pendiente" and estado != "confirmado":
        db.libros.update_one(
            {"_id": ap["libro_id"]}, {"$inc": {"ejemplares_disponibles": 1}}
        )
    db.apartados.update_one(
        {"_id": ObjectId(apartado_id)}, {"$set": {"estado": estado}}
    )
    flash("Estado actualizado.", "success")
    return redirect(url_for("admin_panel"))


@app.route("/admin/prestamo", methods=["POST"])
@admin_required
def admin_prestamo():
    matricula = request.form["matricula"].strip().upper()
    usuario = db.usuarios.find_one({"matricula": matricula})
    if not usuario:
        flash("Matrícula no encontrada.", "danger")
        return redirect(url_for("admin_panel"))

    libro_id = request.form.get("libro_id", "")
    libro = db.libros.find_one({"_id": ObjectId(libro_id)}) if libro_id else None
    if not libro:
        flash("Selecciona un libro válido.", "danger")
        return redirect(url_for("admin_panel"))

    db.prestamos.insert_one({
        "usuario_id": usuario["_id"],
        "usuario_nombre": usuario["nombre"],
        "matricula": matricula,
        "libro_id": libro["_id"],
        "libro_titulo": libro["titulo"],
        "fecha_prestamo": ahora(),
        "fecha_devolucion": None,
        "estado": "activo",
    })
    flash(f"Préstamo registrado para {usuario['nombre']}.", "success")
    return redirect(url_for("admin_panel"))


@app.route("/admin/prestamo/<prestamo_id>/devolver", methods=["POST"])
@admin_required
def admin_devolver(prestamo_id):
    db.prestamos.update_one(
        {"_id": ObjectId(prestamo_id)},
        {"$set": {"estado": "devuelto", "fecha_devolucion": ahora()}},
    )
    flash("Devolución registrada.", "success")
    return redirect(url_for("admin_panel"))


# ------------------------------------------------------------------
# Arranque
# ------------------------------------------------------------------
with app.app_context():
    init_db()


if __name__ == "__main__":
    app.run(debug=True)