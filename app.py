"""Aplicación web para la gestión de la Iglesia Metodista de Jaruco."""

from datetime import date, datetime, timedelta, timezone
from functools import wraps
from html import escape
from io import BytesIO
import json
import re
from urllib.parse import urljoin, urlparse

from bson import ObjectId
from bson.errors import InvalidId
from flask import Flask, abort, flash, redirect, render_template, request, send_file, session, url_for
from flask_bcrypt import Bcrypt
from flask_wtf.csrf import CSRFProtect
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from pymongo import DESCENDING, MongoClient
from pymongo.errors import PyMongoError
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_RIGHT
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from config import Config

app = Flask(__name__)
app.config.from_object(Config)


def _validar_configuracion(configuracion):
	secreto = configuracion.get("SECRET_KEY", "")
	if not secreto or len(secreto) < 32:
		raise RuntimeError("SECRET_KEY debe estar definida con al menos 32 caracteres aleatorios en el archivo .env.")
	if configuracion.get("FLASK_ENV") == "production":
		uri_mongo = configuracion.get("MONGO_URI", "")
		if not uri_mongo or re.search(r"(localhost|127\.0\.0\.1)", uri_mongo, re.IGNORECASE):
			raise RuntimeError("En producción MONGO_URI debe apuntar a una base de datos accesible en la nube, como MongoDB Atlas.")


_validar_configuracion(app.config)
bcrypt = Bcrypt(app)
csrf = CSRFProtect(app)

mongo_client = None
db = None

ROLES = {"administrador", "tesorero", "secretario", "pastor"}
ROLES_ETIQUETAS = {
	"administrador": "Administrador",
	"tesorero": "Tesorero",
	"secretario": "Secretario",
	"pastor": "Pastor",
}
ACTIVIDADES = {
	"culto": "Culto",
	"estudio_biblico": "Estudio bíblico",
	"celebracion": "Celebración",
	"otro": "Otro",
}


def get_db():
	global mongo_client, db
	if not app.config.get("MONGO_URI"):
		raise RuntimeError("MongoDB no está configurado. Añade MONGO_URI al archivo .env.")
	if db is None:
		try:
			mongo_client = MongoClient(app.config["MONGO_URI"], serverSelectionTimeoutMS=5000)
			db = mongo_client[app.config["MONGO_DB_NAME"]]
			db.command("ping")
		except PyMongoError as exc:
			raise RuntimeError("No se pudo conectar a MongoDB. Revisa MONGO_URI y que el servidor esté disponible.") from exc
	return db


@app.after_request
def agregar_cabeceras_seguridad(respuesta):
	respuesta.headers.setdefault("X-Content-Type-Options", "nosniff")
	respuesta.headers.setdefault("X-Frame-Options", "DENY")
	respuesta.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
	respuesta.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
	if app.config.get("FLASK_ENV") == "production":
		respuesta.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
	return respuesta


def login_required(view):
	@wraps(view)
	def wrapped_view(*args, **kwargs):
		if not session.get("usuario"):
			return redirect(url_for("login", next=request.url))
		if app.config.get("AUTH_SESSION_DB_CHECK", True):
			try:
				cuenta = get_db().usuarios.find_one({"usuario": session["usuario"]})
			except (RuntimeError, PyMongoError):
				app.logger.exception("No fue posible verificar el estado de la sesión")
				return "No se pudo verificar la sesión. Inténtalo de nuevo cuando MongoDB esté disponible.", 503
			if not cuenta or cuenta.get("activo", True) is False or cuenta.get("rol") not in ROLES:
				session.clear()
				flash("La cuenta ya no está activa. Inicia sesión con una cuenta válida.", "error")
				return redirect(url_for("login", next=request.url))
			session["rol"] = cuenta["rol"]
			session["nombre"] = cuenta.get("nombre", cuenta["usuario"])
		return view(*args, **kwargs)

	return wrapped_view


def roles_permitidos(*roles):
	def decorator(view):
		@wraps(view)
		@login_required
		def wrapped_view(*args, **kwargs):
			if session.get("rol") not in roles:
				abort(403)
			return view(*args, **kwargs)

		return wrapped_view

	return decorator


def _es_destino_local(destino):
	if not destino:
		return False
	referencia = urlparse(urljoin(request.host_url, destino))
	origen = urlparse(request.host_url)
	return referencia.scheme in {"http", "https"} and referencia.netloc == origen.netloc


def _validar_movimiento(formulario):
	tipo = formulario.get("tipo", "").strip().lower()
	categoria = formulario.get("categoria", "").strip()
	descripcion = formulario.get("descripcion", "").strip()
	try:
		monto = float(formulario.get("monto", ""))
		fecha = datetime.strptime(formulario.get("fecha", ""), "%Y-%m-%d").date()
	except (TypeError, ValueError):
		raise ValueError("Indica un monto válido y una fecha correcta.") from None

	if tipo not in {"ingreso", "gasto"}:
		raise ValueError("El tipo debe ser ingreso o gasto.")
	if monto <= 0:
		raise ValueError("El monto debe ser mayor que cero.")
	if not categoria or len(categoria) > 60:
		raise ValueError("La categoría es obligatoria y no puede superar 60 caracteres.")
	if not descripcion or len(descripcion) > 120:
		raise ValueError("La descripción es obligatoria y no puede superar 120 caracteres.")

	return {
		"tipo": tipo,
		"categoria": categoria,
		"monto": round(monto, 2),
		"fecha": fecha.isoformat(),
		"descripcion": descripcion,
	}


def _validar_miembro(formulario):
	nombre = formulario.get("nombre", "").strip()
	contacto = formulario.get("contacto", "").strip()
	grupo = formulario.get("grupo", "").strip()
	fecha_ingreso = formulario.get("fecha_ingreso", "").strip() or date.today().isoformat()
	if not nombre or len(nombre) > 120:
		raise ValueError("El nombre es obligatorio y no puede superar 120 caracteres.")
	if len(contacto) > 120:
		raise ValueError("El contacto no puede superar 120 caracteres.")
	if len(grupo) > 80:
		raise ValueError("El grupo no puede superar 80 caracteres.")
	try:
		datetime.strptime(fecha_ingreso, "%Y-%m-%d")
	except ValueError:
		raise ValueError("Indica una fecha de ingreso válida.") from None
	return {
		"nombre": nombre,
		"contacto": contacto,
		"grupo": grupo,
		"fecha_ingreso": fecha_ingreso,
	}


def _validar_asistencia(formulario):
	fecha = formulario.get("fecha", "").strip()
	tipo = formulario.get("tipo", "").strip()
	notas = formulario.get("notas", "").strip()
	try:
		datetime.strptime(fecha, "%Y-%m-%d")
		cantidad = int(formulario.get("cantidad", ""))
	except (TypeError, ValueError):
		raise ValueError("Indica una fecha válida y una cantidad entera de asistentes.") from None
	if tipo not in ACTIVIDADES:
		raise ValueError("Selecciona un tipo de actividad válido.")
	if cantidad < 0 or cantidad > 5000:
		raise ValueError("La cantidad debe estar entre 0 y 5000 asistentes.")
	if len(notas) > 500:
		raise ValueError("Las notas no pueden superar 500 caracteres.")
	return {"fecha": fecha, "tipo": tipo, "cantidad": cantidad, "notas": notas}


def _validar_usuario(formulario, exigir_contrasena=False):
	nombre = formulario.get("nombre", "").strip()
	usuario = formulario.get("usuario", "").strip().lower()
	rol = formulario.get("rol", "").strip().lower()
	contrasena = formulario.get("contrasena", "")
	if not nombre or len(nombre) > 120:
		raise ValueError("El nombre es obligatorio y no puede superar 120 caracteres.")
	if not re.fullmatch(r"[a-z0-9._-]{3,40}", usuario):
		raise ValueError("El usuario debe tener 3–40 caracteres: letras, números, punto, guion o guion bajo.")
	if rol not in ROLES:
		raise ValueError("Selecciona un rol válido.")
	if exigir_contrasena and len(contrasena) < 12:
		raise ValueError("La contraseña debe tener al menos 12 caracteres.")
	if contrasena and len(contrasena) < 12:
		raise ValueError("La contraseña debe tener al menos 12 caracteres.")
	if len(contrasena.encode("utf-8")) > 72:
		raise ValueError("La contraseña no puede superar 72 bytes UTF-8.")
	return {"nombre": nombre, "usuario": usuario, "rol": rol, "contrasena": contrasena}


def _hay_otro_administrador(database, usuario_excluido):
	return database.usuarios.count_documents({
		"rol": "administrador",
		"activo": {"$ne": False},
		"usuario": {"$ne": usuario_excluido},
	}) > 0


def _registrar_auditoria(accion, movimiento_id=None, antes=None, despues=None, entidad=None, entidad_id=None):
	get_db().auditoria.insert_one({
		"accion": accion,
		"movimiento_id": movimiento_id,
		"entidad": entidad or ("movimiento" if movimiento_id else None),
		"entidad_id": entidad_id or movimiento_id,
		"usuario": session.get("usuario"),
		"fecha": datetime.now(timezone.utc),
		"antes": antes,
		"despues": despues,
	})


def _limpiar_datos_auditoria(valor):
	if isinstance(valor, dict):
		return {
			str(clave): _limpiar_datos_auditoria(contenido)
			for clave, contenido in valor.items()
			if not any(palabra in str(clave).lower() for palabra in ("password", "contrasena", "secret", "token"))
		}
	if isinstance(valor, (list, tuple)):
		return [_limpiar_datos_auditoria(item) for item in valor]
	return valor


@app.template_filter("money")
def formato_dinero(valor):
	return f"${float(valor or 0):,.2f}"


@app.template_filter("audit_json")
def formato_auditoria(valor):
	return json.dumps(_limpiar_datos_auditoria(valor), ensure_ascii=False, indent=2, default=str)


@app.template_filter("date_es")
def formato_fecha(valor):
	try:
		return datetime.strptime(str(valor), "%Y-%m-%d").strftime("%d %b")
	except ValueError:
		return str(valor)


@app.template_filter("input_date")
def fecha_input(valor):
	if isinstance(valor, datetime):
		return valor.date().isoformat()
	return str(valor)


@app.route("/login", methods=["GET", "POST"])
def login():
	destino = request.args.get("next", "")
	if request.method == "GET" and session.get("usuario"):
		return redirect(destino if _es_destino_local(destino) else url_for("inicio"))
	if request.method == "POST":
		session.clear()
		destino = request.form.get("next", "")
		usuario = request.form.get("usuario", "").strip().lower()
		contrasena = request.form.get("contrasena", "")
		try:
			registro = get_db().usuarios.find_one({"usuario": usuario})
		except (RuntimeError, PyMongoError):
			app.logger.exception("No fue posible consultar la base de datos durante el inicio de sesión")
			flash("No se pudo conectar con el servicio. Inténtalo de nuevo más tarde.", "error")
		else:
			if registro and registro.get("activo", True) is not False and bcrypt.check_password_hash(registro["contrasena"], contrasena):
				session.clear()
				session.update({
					"usuario": registro["usuario"],
					"nombre": registro.get("nombre", registro["usuario"]),
					"rol": registro.get("rol", ""),
				})
				session.permanent = True
				if session["rol"] not in ROLES:
					session.clear()
					flash("La cuenta no tiene un rol válido. Contacta al administrador.", "error")
				else:
					return redirect(destino if _es_destino_local(destino) else url_for("inicio"))
			else:
				flash("Usuario o contraseña incorrectos.", "error")
	return render_template("login.html", next_url=destino)


@app.route("/logout", methods=["POST"])
def logout():
	session.clear()
	return redirect(url_for("login"))


@app.route("/health")
def health():
	try:
		get_db().command("ping")
	except (RuntimeError, PyMongoError):
		return {"status": "unavailable", "database": "unavailable"}, 503
	return {"status": "ok", "database": "ok"}


@app.route("/")
@app.route("/inicio")
@login_required
def inicio():
	hoy = date.today()
	inicio_mes = hoy.replace(day=1).isoformat()
	proximo_mes = (hoy.replace(day=28) + timedelta(days=4)).replace(day=1).isoformat()
	base_filtro = {"anulado": {"$ne": True}, "fecha": {"$gte": inicio_mes, "$lt": proximo_mes}}
	try:
		database = get_db()
		totales = list(database.movimientos.aggregate([
			{"$match": base_filtro},
			{"$group": {"_id": "$tipo", "total": {"$sum": "$monto"}}},
		]))
		por_tipo = {item["_id"]: item["total"] for item in totales}
		ingresos = por_tipo.get("ingreso", 0)
		gastos = por_tipo.get("gasto", 0)
		movimientos = list(database.movimientos.find({"anulado": {"$ne": True}}).sort("fecha", DESCENDING).limit(5))
		cantidad_miembros = database.miembros.count_documents({})

		seis_semanas_atras = hoy - timedelta(days=41)
		registros_asistencia = list(database.asistencias.find({
			"activo": {"$ne": False},
			"fecha": {"$gte": seis_semanas_atras.isoformat(), "$lte": hoy.isoformat()},
		}).sort("fecha", DESCENDING))
		por_fecha = {}
		for registro in registros_asistencia:
			por_fecha[registro["fecha"]] = por_fecha.get(registro["fecha"], 0) + int(registro.get("cantidad", 0))
		asistencia = {"etiquetas": [], "valores": []}
		for semanas in reversed(range(6)):
			domingo = hoy - timedelta(days=(hoy.weekday() + 1) % 7 + 7 * semanas)
			asistencia["etiquetas"].append(domingo.strftime("%d %b"))
			asistencia["valores"].append(por_fecha.get(domingo.isoformat(), 0))
	except (RuntimeError, PyMongoError):
		app.logger.exception("No fue posible cargar el panel de inicio")
		flash("No se pudieron cargar los datos. Comprueba la conexión con MongoDB.", "error")
		ingresos, gastos, cantidad_miembros = 0, 0, 0
		movimientos = []
		asistencia = {"etiquetas": [], "valores": []}

	return render_template(
		"inicio.html",
		periodo=hoy.strftime("%B %Y"),
		resumen={"ingresos": ingresos, "gastos": gastos, "balance": ingresos - gastos, "miembros": cantidad_miembros},
		movimientos=movimientos,
		asistencia=asistencia,
	)


@app.route("/economia")
@login_required
def economia():
	filtros = {
		"mes": request.args.get("mes", "").strip(),
		"tipo": request.args.get("tipo", "").strip(),
		"categoria": request.args.get("categoria", "").strip(),
	}
	consulta = {"anulado": {"$ne": True}}
	if filtros["mes"]:
		try:
			datetime.strptime(filtros["mes"], "%Y-%m")
			consulta["fecha"] = {"$regex": f"^{filtros['mes']}"}
		except ValueError:
			filtros["mes"] = ""
	if filtros["tipo"] in {"ingreso", "gasto"}:
		consulta["tipo"] = filtros["tipo"]
	else:
		filtros["tipo"] = ""
	if filtros["categoria"]:
		consulta["categoria"] = filtros["categoria"]

	try:
		database = get_db()
		movimientos = list(database.movimientos.find(consulta).sort("fecha", DESCENDING).limit(300))
		categorias = sorted(database.movimientos.distinct("categoria", {"anulado": {"$ne": True}}))
	except (RuntimeError, PyMongoError):
		app.logger.exception("No fue posible cargar la lista de movimientos")
		flash("No se pudo cargar la economía. Comprueba la conexión con MongoDB.", "error")
		movimientos, categorias = [], []
	return render_template("economia.html", movimientos=movimientos, categorias=categorias, filtros=filtros)


@app.route("/economia/nuevo", methods=["GET", "POST"])
@roles_permitidos("administrador", "tesorero")
def nuevo_movimiento():
	if request.method == "POST":
		try:
			datos = _validar_movimiento(request.form)
			datos.update({"creado_por": session["usuario"], "anulado": False, "creado_en": datetime.now(timezone.utc)})
			resultado = get_db().movimientos.insert_one(datos)
			_registrar_auditoria("crear_movimiento", resultado.inserted_id, despues=datos)
		except ValueError as error:
			flash(str(error), "error")
		except (RuntimeError, PyMongoError):
			app.logger.exception("No fue posible guardar el movimiento")
			flash("No se pudo guardar el movimiento. Comprueba la conexión con MongoDB.", "error")
		else:
			flash("Movimiento registrado.", "success")
			return redirect(url_for("economia"))
	return render_template("movimiento_form.html", movimiento=None, hoy=date.today().isoformat())


@app.route("/economia/editar/<movimiento_id>", methods=["GET", "POST"])
@roles_permitidos("administrador", "tesorero")
def editar_movimiento(movimiento_id):
	try:
		oid = ObjectId(movimiento_id)
		movimiento = get_db().movimientos.find_one({"_id": oid, "anulado": {"$ne": True}})
	except (InvalidId, RuntimeError, PyMongoError):
		abort(404)
	if movimiento is None:
		abort(404)

	if request.method == "POST":
		try:
			datos = _validar_movimiento(request.form)
			antes = {clave: movimiento.get(clave) for clave in datos}
			get_db().movimientos.update_one({"_id": oid}, {"$set": {**datos, "editado_por": session["usuario"], "editado_en": datetime.now(timezone.utc)}})
			_registrar_auditoria("editar_movimiento", oid, antes=antes, despues=datos)
		except ValueError as error:
			flash(str(error), "error")
		except (RuntimeError, PyMongoError):
			app.logger.exception("No fue posible actualizar el movimiento")
			flash("No se pudo actualizar el movimiento.", "error")
		else:
			flash("Movimiento actualizado.", "success")
			return redirect(url_for("economia"))
	return render_template("movimiento_form.html", movimiento=movimiento, hoy=date.today().isoformat())


@app.route("/economia/anular/<movimiento_id>", methods=["POST"])
@roles_permitidos("administrador", "tesorero")
def anular_movimiento(movimiento_id):
	motivo = request.form.get("motivo", "").strip()
	if not motivo or len(motivo) > 300:
		flash("Escribe un motivo de hasta 300 caracteres para anular el movimiento.", "error")
		return redirect(url_for("economia"))
	try:
		oid = ObjectId(movimiento_id)
		database = get_db()
		movimiento = database.movimientos.find_one({"_id": oid, "anulado": {"$ne": True}})
		if movimiento is None:
			abort(404)
		actualizado = {
			"anulado": True,
			"anulado_por": session["usuario"],
			"anulado_en": datetime.now(timezone.utc),
			"motivo": motivo,
		}
		database.movimientos.update_one({"_id": oid}, {"$set": actualizado})
		_registrar_auditoria("anular_movimiento", oid, antes=movimiento, despues=actualizado)
	except InvalidId:
		abort(404)
	except (RuntimeError, PyMongoError):
		app.logger.exception("No fue posible anular el movimiento")
		flash("No se pudo anular el movimiento.", "error")
	else:
		flash("Movimiento anulado y conservado en el historial.", "success")
	return redirect(url_for("economia"))


@app.route("/asistencia")
@login_required
def asistencia():
	filtros = {
		"mes": request.args.get("mes", "").strip(),
		"tipo": request.args.get("tipo", "").strip(),
	}
	consulta = {"activo": {"$ne": False}}
	if filtros["mes"]:
		try:
			datetime.strptime(filtros["mes"], "%Y-%m")
			consulta["fecha"] = {"$regex": f"^{re.escape(filtros['mes'])}"}
		except ValueError:
			filtros["mes"] = ""
	if filtros["tipo"] in ACTIVIDADES:
		consulta["tipo"] = filtros["tipo"]
	else:
		filtros["tipo"] = ""

	try:
		registros = list(get_db().asistencias.find(consulta).sort("fecha", DESCENDING).limit(300))
	except (RuntimeError, PyMongoError):
		app.logger.exception("No fue posible cargar la lista de asistencia")
		flash("No se pudo cargar la asistencia. Comprueba la conexión con MongoDB.", "error")
		registros = []
	return render_template(
		"asistencia.html",
		registros=registros,
		filtros=filtros,
		actividades=ACTIVIDADES,
		puede_editar=session.get("rol") in {"administrador", "secretario"},
	)


@app.route("/asistencia/nueva", methods=["GET", "POST"])
@roles_permitidos("administrador", "secretario")
def nueva_asistencia():
	if request.method == "POST":
		try:
			datos = _validar_asistencia(request.form)
			datos.update({
				"activo": True,
				"creado_por": session["usuario"],
				"creado_en": datetime.now(timezone.utc),
			})
			resultado = get_db().asistencias.insert_one(datos)
			_registrar_auditoria(
				"crear_asistencia", antes=None, despues=datos,
				entidad="asistencia", entidad_id=resultado.inserted_id,
			)
		except ValueError as error:
			flash(str(error), "error")
		except (RuntimeError, PyMongoError):
			app.logger.exception("No fue posible guardar la asistencia")
			flash("No se pudo guardar el registro de asistencia.", "error")
		else:
			flash("Asistencia registrada.", "success")
			return redirect(url_for("asistencia"))
	return render_template("asistencia_form.html", registro=None, hoy=date.today().isoformat(), actividades=ACTIVIDADES)


@app.route("/asistencia/editar/<registro_id>", methods=["GET", "POST"])
@roles_permitidos("administrador", "secretario")
def editar_asistencia(registro_id):
	try:
		oid = ObjectId(registro_id)
		registro = get_db().asistencias.find_one({"_id": oid, "activo": {"$ne": False}})
	except (InvalidId, RuntimeError, PyMongoError):
		abort(404)
	if registro is None:
		abort(404)

	if request.method == "POST":
		try:
			datos = _validar_asistencia(request.form)
			antes = {clave: registro.get(clave, "") for clave in datos}
			despues = {**datos, "editado_por": session["usuario"], "editado_en": datetime.now(timezone.utc)}
			get_db().asistencias.update_one({"_id": oid}, {"$set": despues})
			_registrar_auditoria(
				"editar_asistencia", antes=antes, despues=datos,
				entidad="asistencia", entidad_id=oid,
			)
		except ValueError as error:
			flash(str(error), "error")
		except (RuntimeError, PyMongoError):
			app.logger.exception("No fue posible actualizar la asistencia")
			flash("No se pudo actualizar el registro de asistencia.", "error")
		else:
			flash("Registro de asistencia actualizado.", "success")
			return redirect(url_for("asistencia"))
	return render_template("asistencia_form.html", registro=registro, hoy=date.today().isoformat(), actividades=ACTIVIDADES)


@app.route("/asistencia/archivar/<registro_id>", methods=["POST"])
@roles_permitidos("administrador", "secretario")
def archivar_asistencia(registro_id):
	try:
		oid = ObjectId(registro_id)
		database = get_db()
		registro = database.asistencias.find_one({"_id": oid, "activo": {"$ne": False}})
		if registro is None:
			abort(404)
		actualizacion = {
			"activo": False,
			"archivado_por": session["usuario"],
			"archivado_en": datetime.now(timezone.utc),
		}
		database.asistencias.update_one({"_id": oid}, {"$set": actualizacion})
		_registrar_auditoria(
			"archivar_asistencia", antes={"activo": registro.get("activo", True)},
			despues=actualizacion, entidad="asistencia", entidad_id=oid,
		)
	except InvalidId:
		abort(404)
	except (RuntimeError, PyMongoError):
		app.logger.exception("No fue posible archivar el registro de asistencia")
		flash("No se pudo archivar el registro de asistencia.", "error")
	else:
		flash("Registro de asistencia archivado y conservado.", "success")
	return redirect(url_for("asistencia"))


@app.route("/miembros")
@login_required
def miembros():
	busqueda = request.args.get("q", "").strip()[:80]
	consulta = {"activo": {"$ne": False}}
	if busqueda:
		patron = re.escape(busqueda)
		consulta["$or"] = [
			{"nombre": {"$regex": patron, "$options": "i"}},
			{"grupo": {"$regex": patron, "$options": "i"}},
			{"contacto": {"$regex": patron, "$options": "i"}},
		]
	try:
		lista = list(get_db().miembros.find(consulta).sort("nombre", 1).limit(500))
	except (RuntimeError, PyMongoError):
		app.logger.exception("No fue posible cargar los miembros")
		flash("No se pudo cargar la lista de miembros.", "error")
		lista = []
	return render_template(
		"miembros.html",
		miembros=lista,
		busqueda=busqueda,
		puede_editar=session.get("rol") in {"administrador", "secretario"},
	)


@app.route("/miembros/nuevo", methods=["GET", "POST"])
@roles_permitidos("administrador", "secretario")
def nuevo_miembro():
	if request.method == "POST":
		try:
			datos = _validar_miembro(request.form)
			datos.update({"activo": True, "creado_por": session["usuario"], "creado_en": datetime.now(timezone.utc)})
			resultado = get_db().miembros.insert_one(datos)
			_registrar_auditoria(
				"crear_miembro", antes=None, despues=datos,
				entidad="miembro", entidad_id=resultado.inserted_id,
			)
		except ValueError as error:
			flash(str(error), "error")
		except (RuntimeError, PyMongoError):
			app.logger.exception("No fue posible guardar el miembro")
			flash("No se pudo guardar el miembro. Comprueba la conexión con MongoDB.", "error")
		else:
			flash("Miembro registrado.", "success")
			return redirect(url_for("miembros"))
	return render_template("miembro_form.html", miembro=None, hoy=date.today().isoformat())


@app.route("/miembros/editar/<miembro_id>", methods=["GET", "POST"])
@roles_permitidos("administrador", "secretario")
def editar_miembro(miembro_id):
	try:
		oid = ObjectId(miembro_id)
		miembro = get_db().miembros.find_one({"_id": oid, "activo": {"$ne": False}})
	except (InvalidId, RuntimeError, PyMongoError):
		abort(404)
	if miembro is None:
		abort(404)

	if request.method == "POST":
		try:
			datos = _validar_miembro(request.form)
			antes = {clave: miembro.get(clave, "") for clave in datos}
			despues = {**datos, "editado_por": session["usuario"], "editado_en": datetime.now(timezone.utc)}
			get_db().miembros.update_one({"_id": oid}, {"$set": despues})
			_registrar_auditoria(
				"editar_miembro", antes=antes, despues=datos,
				entidad="miembro", entidad_id=oid,
			)
		except ValueError as error:
			flash(str(error), "error")
		except (RuntimeError, PyMongoError):
			app.logger.exception("No fue posible actualizar el miembro")
			flash("No se pudo actualizar el miembro.", "error")
		else:
			flash("Miembro actualizado.", "success")
			return redirect(url_for("miembros"))
	return render_template("miembro_form.html", miembro=miembro, hoy=date.today().isoformat())

@app.route("/miembros/archivar/<miembro_id>", methods=["POST"])
@roles_permitidos("administrador", "secretario")
def archivar_miembro(miembro_id):
	try:
		oid = ObjectId(miembro_id)
		database = get_db()
		miembro = database.miembros.find_one({"_id": oid, "activo": {"$ne": False}})
		if miembro is None:
			abort(404)
		actualizacion = {
			"activo": False,
			"archivado_por": session["usuario"],
			"archivado_en": datetime.now(timezone.utc),
		}
		database.miembros.update_one({"_id": oid}, {"$set": actualizacion})
		_registrar_auditoria(
			"archivar_miembro", antes={"activo": miembro.get("activo", True)},
			despues=actualizacion, entidad="miembro", entidad_id=oid,
		)
	except InvalidId:
		abort(404)
	except (RuntimeError, PyMongoError):
		app.logger.exception("No fue posible archivar el miembro")
		flash("No se pudo archivar el miembro.", "error")
	else:
		flash("Miembro archivado; sus datos se conservaron.", "success")
	return redirect(url_for("miembros"))


def _validar_periodo_informe(desde_texto, hasta_texto):
	hoy = date.today()
	desde = hoy.replace(day=1) if not desde_texto else datetime.strptime(desde_texto, "%Y-%m-%d").date()
	hasta = hoy if not hasta_texto else datetime.strptime(hasta_texto, "%Y-%m-%d").date()
	if desde > hasta:
		raise ValueError("La fecha inicial no puede ser posterior a la fecha final.")
	return desde.isoformat(), hasta.isoformat()


def _obtener_datos_informe(desde, hasta, limite_movimientos=200):
	database = get_db()
	consulta = {
		"anulado": {"$ne": True},
		"fecha": {"$gte": desde, "$lte": hasta},
	}
	por_tipo = list(database.movimientos.aggregate([
		{"$match": consulta},
		{"$group": {"_id": "$tipo", "total": {"$sum": "$monto"}, "cantidad": {"$sum": 1}}},
	]))
	totales = {item["_id"]: item for item in por_tipo}
	ingresos = float(totales.get("ingreso", {}).get("total", 0))
	gastos = float(totales.get("gasto", {}).get("total", 0))
	cantidad = sum(int(item.get("cantidad", 0)) for item in por_tipo)
	categorias = list(database.movimientos.aggregate([
		{"$match": consulta},
		{"$group": {
			"_id": {
				"tipo": "$tipo",
				"categoria": {"$ifNull": ["$categoria", "Sin categoría"]},
			},
			"total": {"$sum": "$monto"},
			"cantidad": {"$sum": 1},
		}},
		{"$sort": {"_id.tipo": 1, "_id.categoria": 1}},
	]))
	movimientos = list(database.movimientos.find(consulta).sort([("fecha", DESCENDING), ("_id", DESCENDING)]).limit(limite_movimientos))
	return {
		"resumen": {
			"ingresos": ingresos,
			"gastos": gastos,
			"balance": ingresos - gastos,
			"cantidad": cantidad,
		},
		"categorias": categorias,
		"movimientos": movimientos,
		"truncado": cantidad > len(movimientos),
	}


def _nombre_actividad_economica(tipo):
	return {"ingreso": "Ingreso", "gasto": "Gasto"}.get(tipo, str(tipo).capitalize())


def _valor_excel_seguro(valor):
	if isinstance(valor, str) and valor.startswith(("=", "+", "-", "@")):
		return "'" + valor
	return valor


def _crear_excel_informe(desde, hasta, datos):
	libro = Workbook()
	resumen_ws = libro.active
	resumen_ws.title = "Resumen"
	resumen_ws.append(["Informe económico de la iglesia"])
	resumen_ws.append(["Periodo", desde, "a", hasta])
	resumen_ws.append([])
	resumen_ws.append(["Indicador", "Valor"])
	for celda in resumen_ws[4]:
		celda.font = Font(bold=True, color="FFFFFF")
		celda.fill = PatternFill("solid", fgColor="3569D4")
	for etiqueta, valor in (
		("Ingresos", datos["resumen"]["ingresos"]),
		("Gastos", datos["resumen"]["gastos"]),
		("Balance", datos["resumen"]["balance"]),
		("Movimientos", datos["resumen"]["cantidad"]),
	):
		resumen_ws.append([etiqueta, valor])
		if etiqueta != "Movimientos":
			resumen_ws.cell(resumen_ws.max_row, 2).number_format = '"$"#,##0.00'
	resumen_ws.append([])
	resumen_ws.append(["Tipo", "Categoría", "Cantidad", "Total"])
	for celda in resumen_ws[resumen_ws.max_row]:
		celda.font = Font(bold=True, color="FFFFFF")
		celda.fill = PatternFill("solid", fgColor="3569D4")
	for fila in datos["categorias"]:
		resumen_ws.append([
			_valor_excel_seguro(_nombre_actividad_economica(fila["_id"]["tipo"])),
			_valor_excel_seguro(str(fila["_id"]["categoria"])),
			fila["cantidad"],
			fila["total"],
		])
		resumen_ws.cell(resumen_ws.max_row, 4).number_format = '"$"#,##0.00'
	resumen_ws.column_dimensions["A"].width = 24
	resumen_ws.column_dimensions["B"].width = 28
	resumen_ws.column_dimensions["C"].width = 16
	resumen_ws.column_dimensions["D"].width = 18

	detalle_ws = libro.create_sheet("Movimientos")
	detalle_ws.append(["Fecha", "Tipo", "Categoría", "Descripción", "Monto", "Registrado por"])
	for celda in detalle_ws[1]:
		celda.font = Font(bold=True, color="FFFFFF")
		celda.fill = PatternFill("solid", fgColor="3569D4")
	for movimiento in datos["movimientos"]:
		detalle_ws.append([
			movimiento.get("fecha", ""),
			_nombre_actividad_economica(movimiento.get("tipo", "")),
			movimiento.get("categoria", ""),
			movimiento.get("descripcion", ""),
			float(movimiento.get("monto", 0)),
			movimiento.get("creado_por", ""),
		])
		detalle_ws.cell(detalle_ws.max_row, 5).number_format = '"$"#,##0.00'
		# Prevenir que texto controlado por el usuario se evalúe como fórmula en Excel.
		for columna in (2, 3, 4, 6):
			celda = detalle_ws.cell(detalle_ws.max_row, columna)
			celda.value = _valor_excel_seguro(celda.value)
	detalle_ws.freeze_panes = "A2"
	detalle_ws.auto_filter.ref = detalle_ws.dimensions
	for columna, ancho in {"A": 14, "B": 16, "C": 22, "D": 44, "E": 16, "F": 20}.items():
		detalle_ws.column_dimensions[columna].width = ancho
	salida = BytesIO()
	libro.save(salida)
	salida.seek(0)
	return salida


def _crear_pdf_informe(desde, hasta, datos):
	salida = BytesIO()
	documento = SimpleDocTemplate(
		salida,
		pagesize=landscape(A4),
		leftMargin=16 * mm,
		rightMargin=16 * mm,
		topMargin=14 * mm,
		bottomMargin=14 * mm,
	)
	estilos = getSampleStyleSheet()
	estilos.add(ParagraphStyle(name="Detalle", parent=estilos["BodyText"], fontSize=7, leading=9, wordWrap="CJK"))
	estilos.add(ParagraphStyle(name="Numero", parent=estilos["BodyText"], fontSize=8, alignment=TA_RIGHT))
	elementos = [
		Paragraph("Informe económico de la iglesia", estilos["Title"]),
		Paragraph(f"Periodo: {desde} a {hasta}", estilos["Normal"]),
		Spacer(1, 7 * mm),
	]
	tabla_resumen = [
		["Ingresos", "Gastos", "Balance", "Movimientos"],
		[
			f"${datos['resumen']['ingresos']:,.2f}",
			f"${datos['resumen']['gastos']:,.2f}",
			f"${datos['resumen']['balance']:,.2f}",
			str(datos["resumen"]["cantidad"]),
		],
	]
	elementos.append(Table(tabla_resumen, colWidths=[55 * mm] * 4, style=TableStyle([
		("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#3569D4")),
		("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
		("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
		("ALIGN", (0, 0), (-1, -1), "CENTER"),
		("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#DCE3EF")),
		("BOTTOMPADDING", (0, 0), (-1, -1), 7),
		("TOPPADDING", (0, 0), (-1, -1), 7),
	])))
	elementos.extend([Spacer(1, 7 * mm), Paragraph("Resumen por categoría", estilos["Heading2"])])
	filas_categoria = [["Tipo", "Categoría", "Cantidad", "Total"]]
	for fila in datos["categorias"]:
		filas_categoria.append([
			_nombre_actividad_economica(fila["_id"]["tipo"]),
			Paragraph(escape(str(fila["_id"]["categoria"])), estilos["Detalle"]),
			str(fila["cantidad"]),
			f"${float(fila['total']):,.2f}",
		])
	elementos.append(Table(filas_categoria, repeatRows=1, colWidths=[38 * mm, 92 * mm, 30 * mm, 38 * mm], style=TableStyle([
		("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#EAF0FB")),
		("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
		("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#DCE3EF")),
		("ALIGN", (2, 1), (-1, -1), "RIGHT"),
		("VALIGN", (0, 0), (-1, -1), "TOP"),
	])))
	elementos.extend([Spacer(1, 7 * mm), Paragraph("Movimientos", estilos["Heading2"])])
	filas_movimientos = [["Fecha", "Tipo", "Categoría", "Descripción", "Monto"]]
	for movimiento in datos["movimientos"]:
		descripcion = escape(str(movimiento.get("descripcion", ""))).replace("\n", "<br/>")
		categoria = escape(str(movimiento.get("categoria", "")))
		filas_movimientos.append([
			str(movimiento.get("fecha", "")),
			_nombre_actividad_economica(movimiento.get("tipo", "")),
			Paragraph(categoria, estilos["Detalle"]),
			Paragraph(descripcion, estilos["Detalle"]),
			Paragraph(f"${float(movimiento.get('monto', 0)):,.2f}", estilos["Numero"]),
		])
	elementos.append(Table(filas_movimientos, repeatRows=1, colWidths=[25 * mm, 32 * mm, 42 * mm, 125 * mm, 29 * mm], style=TableStyle([
		("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#EAF0FB")),
		("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
		("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#DCE3EF")),
		("VALIGN", (0, 0), (-1, -1), "TOP"),
	])))
	if datos["truncado"]:
		elementos.extend([Spacer(1, 3 * mm), Paragraph("Nota: el detalle está limitado a 5.000 movimientos; los totales incluyen todo el periodo.", estilos["Italic"])])
	documento.build(elementos)
	salida.seek(0)
	return salida


@app.route("/informes")
@roles_permitidos("administrador", "tesorero", "pastor")
def informes():
	hoy = date.today()
	desde = request.args.get("desde", "")
	hasta = request.args.get("hasta", "")
	try:
		desde, hasta = _validar_periodo_informe(desde, hasta)
	except ValueError as error:
		flash(str(error), "error")
		desde, hasta = hoy.replace(day=1).isoformat(), hoy.isoformat()

	try:
		datos = _obtener_datos_informe(desde, hasta)
	except (RuntimeError, PyMongoError):
		app.logger.exception("No fue posible generar el informe")
		flash("No se pudo consultar el informe. Comprueba la conexión con MongoDB.", "error")
		datos = {"resumen": {"ingresos": 0, "gastos": 0, "balance": 0, "cantidad": 0}, "categorias": [], "movimientos": [], "truncado": False}
	return render_template("informes.html", desde=desde, hasta=hasta, **datos)


def _descargar_informe(formato):
	try:
		desde, hasta = _validar_periodo_informe(request.args.get("desde", ""), request.args.get("hasta", ""))
		datos = _obtener_datos_informe(desde, hasta, limite_movimientos=5000)
	except ValueError as error:
		flash(str(error), "error")
		return redirect(url_for("informes"))
	except (RuntimeError, PyMongoError):
		app.logger.exception("No fue posible exportar el informe")
		flash("No se pudo exportar el informe. Comprueba la conexión con MongoDB.", "error")
		return redirect(url_for("informes"))

	periodo = f"{desde}_{hasta}"
	if formato == "excel":
		archivo = _crear_excel_informe(desde, hasta, datos)
		return send_file(
			archivo,
			as_attachment=True,
			download_name=f"informe_economico_{periodo}.xlsx",
			mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
		)
	archivo = _crear_pdf_informe(desde, hasta, datos)
	return send_file(archivo, as_attachment=True, download_name=f"informe_economico_{periodo}.pdf", mimetype="application/pdf")


@app.route("/informes/excel")
@roles_permitidos("administrador", "tesorero", "pastor")
def descargar_informe_excel():
	return _descargar_informe("excel")


@app.route("/informes/pdf")
@roles_permitidos("administrador", "tesorero", "pastor")
def descargar_informe_pdf():
	return _descargar_informe("pdf")


@app.route("/admin/usuarios")
@roles_permitidos("administrador")
def usuarios():
	try:
		lista = list(get_db().usuarios.find({}).sort([("activo", DESCENDING), ("nombre", 1)]).limit(500))
	except (RuntimeError, PyMongoError):
		app.logger.exception("No fue posible cargar los usuarios")
		flash("No se pudo cargar la lista de usuarios.", "error")
		lista = []
	return render_template("usuarios.html", usuarios=lista, roles=ROLES_ETIQUETAS)


@app.route("/admin/usuarios/nuevo", methods=["GET", "POST"])
@roles_permitidos("administrador")
def nuevo_usuario():
	if request.method == "POST":
		try:
			datos = _validar_usuario(request.form, exigir_contrasena=True)
			database = get_db()
			database.usuarios.create_index("usuario", unique=True)
			if database.usuarios.find_one({"usuario": datos["usuario"]}):
				raise ValueError("Ese nombre de usuario ya está registrado.")
			usuario_publico = {clave: datos[clave] for clave in ("nombre", "usuario", "rol")}
			registro = {
				**usuario_publico,
				"contrasena": bcrypt.generate_password_hash(datos["contrasena"]).decode("utf-8"),
				"activo": True,
				"creado_por": session["usuario"],
				"creado_en": datetime.now(timezone.utc),
			}
			resultado = database.usuarios.insert_one(registro)
			_registrar_auditoria(
				"crear_usuario", antes=None, despues=usuario_publico,
				entidad="usuario", entidad_id=resultado.inserted_id,
			)
		except ValueError as error:
			flash(str(error), "error")
		except PyMongoError:
			app.logger.exception("No fue posible guardar el usuario")
			flash("No se pudo guardar el usuario; comprueba si el nombre ya existe.", "error")
		except RuntimeError:
			app.logger.exception("No fue posible conectar para crear el usuario")
			flash("No se pudo conectar con MongoDB.", "error")
		else:
			flash("Usuario creado correctamente.", "success")
			return redirect(url_for("usuarios"))
	return render_template("usuario_form.html", usuario=None, roles=ROLES_ETIQUETAS)


@app.route("/admin/usuarios/editar/<usuario_id>", methods=["GET", "POST"])
@roles_permitidos("administrador")
def editar_usuario(usuario_id):
	try:
		oid = ObjectId(usuario_id)
		registro = get_db().usuarios.find_one({"_id": oid})
	except (InvalidId, RuntimeError, PyMongoError):
		abort(404)
	if registro is None:
		abort(404)

	if request.method == "POST":
		try:
			datos = _validar_usuario(request.form)
			database = get_db()
			if database.usuarios.find_one({"usuario": datos["usuario"], "_id": {"$ne": oid}}):
				raise ValueError("Ese nombre de usuario ya está registrado.")
			if registro["usuario"] == session["usuario"] and datos["rol"] != registro.get("rol"):
				raise ValueError("No puedes cambiar tu propio rol desde esta pantalla.")
			if (
				registro.get("activo", True) is not False
				and registro.get("rol") == "administrador"
				and datos["rol"] != "administrador"
				and not _hay_otro_administrador(database, registro["usuario"])
			):
				raise ValueError("Debe quedar al menos un administrador activo.")

			publicos = {clave: datos[clave] for clave in ("nombre", "usuario", "rol")}
			actualizacion = {
				**publicos,
				"editado_por": session["usuario"],
				"editado_en": datetime.now(timezone.utc),
			}
			if datos["contrasena"]:
				actualizacion["contrasena"] = bcrypt.generate_password_hash(datos["contrasena"]).decode("utf-8")
			antes = {clave: registro.get(clave) for clave in publicos}
			database.usuarios.update_one({"_id": oid}, {"$set": actualizacion})
			cambios_auditados = {**publicos, "contrasena_cambiada": bool(datos["contrasena"])}
			_registrar_auditoria(
				"editar_usuario", antes=antes, despues=cambios_auditados,
				entidad="usuario", entidad_id=oid,
			)
		except ValueError as error:
			flash(str(error), "error")
		except PyMongoError:
			app.logger.exception("No fue posible actualizar el usuario")
			flash("No se pudo actualizar el usuario; comprueba si el nombre ya existe.", "error")
		except RuntimeError:
			app.logger.exception("No fue posible conectar para actualizar el usuario")
			flash("No se pudo conectar con MongoDB.", "error")
		else:
			if registro["usuario"] == session["usuario"]:
				session["usuario"] = publicos["usuario"]
				session["nombre"] = publicos["nombre"]
			flash("Usuario actualizado.", "success")
			return redirect(url_for("usuarios"))
	return render_template("usuario_form.html", usuario=registro, roles=ROLES_ETIQUETAS)


@app.route("/admin/usuarios/estado/<usuario_id>", methods=["POST"])
@roles_permitidos("administrador")
def cambiar_estado_usuario(usuario_id):
	try:
		oid = ObjectId(usuario_id)
		database = get_db()
		registro = database.usuarios.find_one({"_id": oid})
		if registro is None:
			abort(404)
		esta_activo = registro.get("activo", True) is not False
		if esta_activo and registro["usuario"] == session["usuario"]:
			flash("No puedes desactivar tu propia cuenta.", "error")
			return redirect(url_for("usuarios"))
		if (
			esta_activo
			and registro.get("rol") == "administrador"
			and not _hay_otro_administrador(database, registro["usuario"])
		):
			flash("Debe quedar al menos un administrador activo.", "error")
			return redirect(url_for("usuarios"))
		activo_nuevo = not esta_activo
		actualizacion = {
			"activo": activo_nuevo,
			"estado_cambiado_por": session["usuario"],
			"estado_cambiado_en": datetime.now(timezone.utc),
		}
		database.usuarios.update_one({"_id": oid}, {"$set": actualizacion})
		_registrar_auditoria(
			"activar_usuario" if activo_nuevo else "desactivar_usuario",
			antes={"activo": esta_activo}, despues={"activo": activo_nuevo},
			entidad="usuario", entidad_id=oid,
		)
	except InvalidId:
		abort(404)
	except (RuntimeError, PyMongoError):
		app.logger.exception("No fue posible cambiar el estado del usuario")
		flash("No se pudo cambiar el estado del usuario.", "error")
	else:
		flash("Usuario activado." if activo_nuevo else "Usuario desactivado.", "success")
	return redirect(url_for("usuarios"))


@app.route("/admin/historial")
@roles_permitidos("administrador")
def historial():
	try:
		eventos = list(get_db().auditoria.find({}).sort("fecha", DESCENDING).limit(200))
	except (RuntimeError, PyMongoError):
		app.logger.exception("No fue posible cargar el historial")
		flash("No se pudo cargar el historial.", "error")
		eventos = []
	return render_template("historial.html", eventos=eventos)


@app.errorhandler(403)
def prohibido(_error):
	return render_template("error.html", codigo=403, mensaje="No tienes permiso para acceder a esta sección."), 403


@app.errorhandler(404)
def no_encontrado(_error):
	return render_template("error.html", codigo=404, mensaje="No encontramos la página o el registro solicitado."), 404


if __name__ == "__main__":
	app.run(debug=app.config.get("ENV") == "development")
