"""Lista administradores locales y permite restablecer la clave de uno.

Usar solo después de iniciar el servicio MongoDB local. No imprime contraseñas
ni toca bases remotas.
"""

import getpass
import os
import sys
from datetime import datetime, timezone

from dotenv import load_dotenv
from flask_bcrypt import Bcrypt
from pymongo import MongoClient
from pymongo.errors import PyMongoError

load_dotenv()


def main() -> int:
    uri = os.getenv("MONGO_URI", "")
    nombre_db = os.getenv("MONGO_DB_NAME", "iglesia")
    if not uri.startswith("mongodb://localhost:27017"):
        print("Por seguridad, este asistente solo funciona con MongoDB local (localhost:27017).", file=sys.stderr)
        return 1
    if nombre_db != "iglesia":
        print("La base configurada no es 'iglesia'; no se hicieron cambios.", file=sys.stderr)
        return 1

    cliente = MongoClient(uri, serverSelectionTimeoutMS=5000)
    try:
        base = cliente[nombre_db]
        base.command("ping")
        administradores = list(base.usuarios.find(
            {"rol": "administrador", "activo": {"$ne": False}},
            {"usuario": 1, "nombre": 1},
        ).sort("usuario", 1))
        if not administradores:
            print("No hay administradores activos en esta base.", file=sys.stderr)
            return 1

        print("Administradores activos:")
        for indice, cuenta in enumerate(administradores, start=1):
            print(f"{indice}. {cuenta.get('nombre', '(sin nombre)')} — @{cuenta['usuario']}")

        seleccion = input("Número de la cuenta cuya contraseña quieres restablecer: ").strip()
        try:
            cuenta = administradores[int(seleccion) - 1]
        except (ValueError, IndexError):
            print("Selección inválida; no se hicieron cambios.", file=sys.stderr)
            return 1

        nueva = getpass.getpass("Nueva contraseña (12–72 bytes UTF-8): ")
        confirmacion = getpass.getpass("Repite la nueva contraseña: ")
        if nueva != confirmacion:
            print("Las contraseñas no coinciden; no se hicieron cambios.", file=sys.stderr)
            return 1
        if len(nueva) < 12 or len(nueva.encode("utf-8")) > 72:
            print("La contraseña debe tener entre 12 y 72 bytes UTF-8.", file=sys.stderr)
            return 1

        hash_nuevo = Bcrypt().generate_password_hash(nueva).decode("utf-8")
        base.usuarios.update_one(
            {"_id": cuenta["_id"], "rol": "administrador", "activo": {"$ne": False}},
            {"$set": {
                "contrasena": hash_nuevo,
                "editado_por": "recuperacion_local",
                "editado_en": datetime.now(timezone.utc),
            }},
        )
        base.auditoria.insert_one({
            "accion": "restablecer_contrasena_administrador",
            "entidad": "usuario",
            "entidad_id": cuenta["_id"],
            "usuario": "recuperacion_local",
            "fecha": datetime.now(timezone.utc),
            "antes": None,
            "despues": {"usuario": cuenta["usuario"], "contrasena_cambiada": True},
        })
        print(f"Contraseña restablecida para @{cuenta['usuario']}. Ya puedes iniciar sesión.")
        return 0
    except PyMongoError as error:
        print(f"No se pudo acceder a MongoDB: {type(error).__name__}. Comprueba que el servicio esté iniciado.", file=sys.stderr)
        return 1
    finally:
        cliente.close()


if __name__ == "__main__":
    raise SystemExit(main())
