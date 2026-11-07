"""Pruebas de integración de usuarios contra una BD desechable.

Ejecutar solo con MONGO_TEST_URI definida. El test crea y elimina una base
con nombre aleatorio; nunca apunta automáticamente a la base de la iglesia.
"""

import os
import unittest
from uuid import uuid4

from pymongo import MongoClient

import app as iglesia_app


MONGO_TEST_URI = os.getenv("MONGO_TEST_URI")


@unittest.skipUnless(MONGO_TEST_URI, "Define MONGO_TEST_URI para habilitar las pruebas MongoDB de integración.")
class PruebasIntegracionUsuarios(unittest.TestCase):
    def setUp(self):
        self.database_name = f"iglesia_test_usuarios_{uuid4().hex}"
        self.mongo_client = MongoClient(MONGO_TEST_URI, serverSelectionTimeoutMS=5000)
        self.database = self.mongo_client[self.database_name]
        self.database.command("ping")

        self.database_original = iglesia_app.db
        self.config_original = {
            clave: iglesia_app.app.config.get(clave)
            for clave in ("TESTING", "WTF_CSRF_ENABLED", "AUTH_SESSION_DB_CHECK", "MONGO_URI", "SECRET_KEY")
        }
        iglesia_app.db = self.database
        iglesia_app.app.config.update(
            TESTING=True,
            WTF_CSRF_ENABLED=False,
            AUTH_SESSION_DB_CHECK=True,
            MONGO_URI=MONGO_TEST_URI,
            SECRET_KEY="clave-de-pruebas-de-integracion",
        )
        self.admin_password = "Admin-Prueba-Segura-123!"
        self.database.usuarios.insert_one({
            "nombre": "Administrador de prueba",
            "usuario": "admin_prueba",
            "rol": "administrador",
            "contrasena": iglesia_app.bcrypt.generate_password_hash(self.admin_password).decode("utf-8"),
            "activo": True,
        })
        self.admin = iglesia_app.app.test_client()
        respuesta = self.admin.post("/login", data={"usuario": "admin_prueba", "contrasena": self.admin_password})
        self.assertEqual(respuesta.status_code, 302)

    def tearDown(self):
        self.mongo_client.drop_database(self.database_name)
        self.mongo_client.close()
        iglesia_app.db = self.database_original
        iglesia_app.app.config.update(self.config_original)

    def crear_usuario(self, usuario="secretaria_prueba", rol="secretario"):
        return self.admin.post("/admin/usuarios/nuevo", data={
            "nombre": "Secretaria de prueba",
            "usuario": usuario,
            "rol": rol,
            "contrasena": "Secretaria-Prueba-123!",
        })

    def iniciar_sesion(self, usuario, contrasena):
        cliente = iglesia_app.app.test_client()
        respuesta = cliente.post("/login", data={"usuario": usuario, "contrasena": contrasena})
        return cliente, respuesta

    def test_crear_usuario_con_hash_login_y_permisos(self):
        respuesta = self.crear_usuario()
        self.assertEqual(respuesta.status_code, 302)

        usuario = self.database.usuarios.find_one({"usuario": "secretaria_prueba"})
        self.assertIsNotNone(usuario)
        self.assertNotEqual(usuario["contrasena"], "Secretaria-Prueba-123!")
        self.assertTrue(iglesia_app.bcrypt.check_password_hash(usuario["contrasena"], "Secretaria-Prueba-123!"))

        duplicado = self.crear_usuario()
        self.assertEqual(duplicado.status_code, 200)
        self.assertEqual(self.database.usuarios.count_documents({"usuario": "secretaria_prueba"}), 1)

        cliente, login = self.iniciar_sesion("secretaria_prueba", "Secretaria-Prueba-123!")
        self.assertEqual(login.status_code, 302)
        self.assertEqual(cliente.get("/miembros/nuevo").status_code, 200)
        self.assertEqual(cliente.get("/admin/usuarios").status_code, 403)

    def test_cambio_de_rol_se_aplica_a_sesion_existente_y_cambia_contrasena(self):
        self.assertEqual(self.crear_usuario().status_code, 302)
        usuario = self.database.usuarios.find_one({"usuario": "secretaria_prueba"})
        cliente, login = self.iniciar_sesion("secretaria_prueba", "Secretaria-Prueba-123!")
        self.assertEqual(login.status_code, 302)

        respuesta = self.admin.post(f"/admin/usuarios/editar/{usuario['_id']}", data={
            "nombre": "Secretaria de prueba",
            "usuario": "secretaria_prueba",
            "rol": "pastor",
            "contrasena": "Nueva-Prueba-Segura-456!",
        })
        self.assertEqual(respuesta.status_code, 302)
        self.assertEqual(cliente.get("/miembros/nuevo").status_code, 403)

        cliente_antiguo, login_antiguo = self.iniciar_sesion("secretaria_prueba", "Secretaria-Prueba-123!")
        self.assertEqual(login_antiguo.status_code, 200)
        cliente_nuevo, login_nuevo = self.iniciar_sesion("secretaria_prueba", "Nueva-Prueba-Segura-456!")
        self.assertEqual(login_nuevo.status_code, 302)
        self.assertEqual(cliente_nuevo.get("/admin/usuarios").status_code, 403)
        self.assertEqual(self.database.auditoria.count_documents({"entidad_id": usuario["_id"]}), 2)

    def test_desactivacion_revoca_sesion_y_no_permite_quedarse_sin_admin(self):
        self.assertEqual(self.crear_usuario().status_code, 302)
        usuario = self.database.usuarios.find_one({"usuario": "secretaria_prueba"})
        cliente, login = self.iniciar_sesion("secretaria_prueba", "Secretaria-Prueba-123!")
        self.assertEqual(login.status_code, 302)

        respuesta = self.admin.post(f"/admin/usuarios/estado/{usuario['_id']}")
        self.assertEqual(respuesta.status_code, 302)
        self.assertEqual(cliente.get("/inicio").status_code, 302)
        login_inactivo = cliente.post("/login", data={"usuario": "secretaria_prueba", "contrasena": "Secretaria-Prueba-123!"})
        self.assertEqual(login_inactivo.status_code, 200)

        respuesta = self.admin.post(f"/admin/usuarios/estado/{self.admin_user_id()}")
        self.assertEqual(respuesta.status_code, 302)
        administrador = self.database.usuarios.find_one({"usuario": "admin_prueba"})
        self.assertTrue(administrador["activo"])
        self.assertEqual(self.database.usuarios.count_documents({"rol": "administrador", "activo": {"$ne": False}}), 1)

    def test_cambio_de_rol_de_otro_admin_revoca_permisos_sin_relogin(self):
        self.assertEqual(self.crear_usuario("segundo_admin", "administrador").status_code, 302)
        segundo = self.database.usuarios.find_one({"usuario": "segundo_admin"})
        cliente, login = self.iniciar_sesion("segundo_admin", "Secretaria-Prueba-123!")
        self.assertEqual(login.status_code, 302)
        self.assertEqual(cliente.get("/admin/usuarios").status_code, 200)

        respuesta = self.admin.post(f"/admin/usuarios/editar/{segundo['_id']}", data={
            "nombre": "Secretaria de prueba",
            "usuario": "segundo_admin",
            "rol": "pastor",
            "contrasena": "",
        })
        self.assertEqual(respuesta.status_code, 302)
        self.assertEqual(cliente.get("/admin/usuarios").status_code, 403)
        self.assertEqual(self.database.usuarios.count_documents({"rol": "administrador", "activo": {"$ne": False}}), 1)

    def test_health_confirma_la_conexion_con_mongo(self):
        respuesta = self.admin.get("/health")

        self.assertEqual(respuesta.status_code, 200)
        self.assertEqual(respuesta.json, {"status": "ok", "database": "ok"})

    def test_historial_muestra_cambios_sin_exponer_hashes(self):
        self.database.auditoria.insert_one({
            "accion": "editar_usuario",
            "entidad": "usuario",
            "usuario": "admin_prueba",
            "fecha": iglesia_app.datetime.now(iglesia_app.timezone.utc),
            "antes": {"rol": "pastor", "contrasena": "hash-antiguo-ultrasecreto"},
            "despues": {"rol": "tesorero", "contrasena": "hash-nuevo-ultrasecreto", "contrasena_cambiada": True},
        })

        respuesta = self.admin.get("/admin/historial")

        self.assertEqual(respuesta.status_code, 200)
        self.assertIn("Ver datos del cambio".encode(), respuesta.data)
        self.assertIn(b"pastor", respuesta.data)
        self.assertIn(b"tesorero", respuesta.data)
        self.assertNotIn(b"ultrasecreto", respuesta.data)

    def admin_user_id(self):
        return self.database.usuarios.find_one({"usuario": "admin_prueba"})["_id"]


if __name__ == "__main__":
    unittest.main(verbosity=2)
