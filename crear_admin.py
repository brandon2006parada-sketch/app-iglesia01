"""Crea el primer usuario administrador de la aplicación."""

import getpass
import os
import sys

from dotenv import load_dotenv
from flask_bcrypt import Bcrypt
from pymongo import MongoClient
from pymongo.errors import PyMongoError

load_dotenv()


def main() -> int:
    mongo_uri = os.getenv("MONGO_URI")
    if not mongo_uri:
        print("Falta MONGO_URI. Configura primero el archivo .env.", file=sys.stderr)
        return 1

    nombre = input("Nombre del administrador: ").strip()
    usuario = input("Nombre de usuario: ").strip().lower()
    contrasena = getpass.getpass("Contraseña (mínimo 12 caracteres): ")

    if not nombre or not usuario:
        print("El nombre y el usuario son obligatorios.", file=sys.stderr)
        return 1
    if len(contrasena) < 12:
        print("La contraseña debe tener al menos 12 caracteres.", file=sys.stderr)
        return 1

    cliente = MongoClient(mongo_uri, serverSelectionTimeoutMS=5000)
    try:
        db = cliente[os.getenv("MONGO_DB_NAME", "iglesia")]
        db.command("ping")
        if db.usuarios.find_one({"usuario": usuario}):
            print("Ese nombre de usuario ya existe.", file=sys.stderr)
            return 1

        bcrypt = Bcrypt()
        hash_contrasena = bcrypt.generate_password_hash(contrasena).decode("utf-8")
        db.usuarios.insert_one({
            "nombre": nombre,
            "usuario": usuario,
            "contrasena": hash_contrasena,
            "rol": "administrador",
        })
        print(f"Administrador '{usuario}' creado correctamente.")
        return 0
    except PyMongoError as error:
        print(f"No se pudo conectar con MongoDB: {error}", file=sys.stderr)
        return 1
    finally:
        cliente.close()


if __name__ == "__main__":
    raise SystemExit(main())
