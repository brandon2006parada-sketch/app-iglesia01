"""Pruebas rápidas de rutas, autenticación y permisos, sin conectar a MongoDB."""

import re
import unittest

import app as iglesia_app


class PruebasDeRutas(unittest.TestCase):
    def setUp(self):
        self.clave_original = iglesia_app.app.config.get("SECRET_KEY")
        self.testing_original = iglesia_app.app.config.get("TESTING")
        self.csrf_original = iglesia_app.app.config.get("WTF_CSRF_ENABLED")
        self.auth_db_check_original = iglesia_app.app.config.get("AUTH_SESSION_DB_CHECK")
        self.uri_original = iglesia_app.app.config.get("MONGO_URI")
        self.db_original = iglesia_app.db

        iglesia_app.app.config.update(
            SECRET_KEY="clave-solo-para-pruebas",
            TESTING=True,
            WTF_CSRF_ENABLED=False,
            AUTH_SESSION_DB_CHECK=False,
            MONGO_URI="",
        )
        # Evita que una prueba contacte accidentalmente la base real.
        iglesia_app.db = None
        self.cliente = iglesia_app.app.test_client()

    def tearDown(self):
        iglesia_app.app.config["SECRET_KEY"] = self.clave_original
        iglesia_app.app.config["TESTING"] = self.testing_original
        iglesia_app.app.config["WTF_CSRF_ENABLED"] = self.csrf_original
        iglesia_app.app.config["AUTH_SESSION_DB_CHECK"] = self.auth_db_check_original
        iglesia_app.app.config["MONGO_URI"] = self.uri_original
        iglesia_app.db = self.db_original

    def iniciar_sesion_como(self, rol):
        with self.cliente.session_transaction() as sesion:
            sesion["usuario"] = "usuario-prueba"
            sesion["nombre"] = "Usuario de prueba"
            sesion["rol"] = rol

    def test_login_muestra_formulario(self):
        respuesta = self.cliente.get("/login")

        self.assertEqual(respuesta.status_code, 200)
        self.assertIn(b"Iniciar sesi\xc3\xb3n", respuesta.data)

    def test_login_rechaza_post_sin_token_csrf(self):
        iglesia_app.app.config["WTF_CSRF_ENABLED"] = True

        respuesta = self.cliente.post("/login", data={"usuario": "x", "contrasena": "x"})

        self.assertEqual(respuesta.status_code, 400)
        self.assertIn(b"CSRF", respuesta.data)

    def test_intento_de_login_fallido_no_conserva_sesion_anterior(self):
        self.iniciar_sesion_como("administrador")

        respuesta = self.cliente.post("/login", data={"usuario": "incorrecto", "contrasena": "incorrecta"})

        self.assertEqual(respuesta.status_code, 200)
        with self.cliente.session_transaction() as sesion:
            self.assertNotIn("usuario", sesion)

    def test_sesion_existente_no_ve_formulario_login(self):
        self.iniciar_sesion_como("administrador")

        respuesta = self.cliente.get("/login")

        self.assertEqual(respuesta.status_code, 302)
        self.assertIn("/inicio", respuesta.headers["Location"])

    def test_formulario_csrf_protege_un_post_de_cierre_de_sesion(self):
        self.iniciar_sesion_como("administrador")
        iglesia_app.app.config["WTF_CSRF_ENABLED"] = True

        formulario = self.cliente.get("/inicio")
        token = re.search(rb'name="csrf_token" value="([^"]+)"', formulario.data)
        self.assertIsNotNone(token)

        respuesta = self.cliente.post("/logout", data={"csrf_token": token.group(1).decode()})

        self.assertEqual(respuesta.status_code, 302)
        self.assertIn("/login", respuesta.headers["Location"])

    def test_login_formulario_incluye_token_csrf(self):
        iglesia_app.app.config["WTF_CSRF_ENABLED"] = True

        respuesta = self.cliente.get("/login")

        self.assertEqual(respuesta.status_code, 200)
        self.assertRegex(respuesta.data.decode(), r'name="csrf_token" value="[^"]+"')

    def test_cabeceras_basicas_de_seguridad_presentes(self):
        respuesta = self.cliente.get("/login")

        self.assertEqual(respuesta.headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(respuesta.headers["X-Frame-Options"], "DENY")

    def test_health_indica_servicio_no_disponible_si_no_hay_mongo(self):
        respuesta = self.cliente.get("/health")

        self.assertEqual(respuesta.status_code, 503)
        self.assertEqual(respuesta.json["status"], "unavailable")

    def test_formateador_de_auditoria_oculta_claves_sensibles(self):
        salida = iglesia_app.formato_auditoria({
            "usuario": "ana",
            "contrasena": "hash-no-debe-mostrarse",
            "detalle": {"access_token": "token-no-debe-mostrarse", "cambio": 1},
        })

        self.assertIn('"usuario": "ana"', salida)
        self.assertIn('"cambio": 1', salida)
        self.assertNotIn("hash-no-debe-mostrarse", salida)
        self.assertNotIn("token-no-debe-mostrarse", salida)

    def test_economia_requiere_inicio_de_sesion(self):
        respuesta = self.cliente.get("/economia")

        self.assertEqual(respuesta.status_code, 302)
        self.assertIn("/login", respuesta.headers["Location"])

    def test_dashboard_se_muestra_a_usuario_autenticado(self):
        self.iniciar_sesion_como("pastor")

        respuesta = self.cliente.get("/")

        self.assertEqual(respuesta.status_code, 200)
        self.assertIn(b"Resumen de", respuesta.data)

    def test_secretario_puede_abrir_asistencia(self):
        self.iniciar_sesion_como("secretario")

        respuesta = self.cliente.get("/asistencia")

        self.assertEqual(respuesta.status_code, 200)
        self.assertIn(b"Asistencia", respuesta.data)

    def test_secretario_puede_abrir_formulario_de_asistencia(self):
        self.iniciar_sesion_como("secretario")

        respuesta = self.cliente.get("/asistencia/nueva")

        self.assertEqual(respuesta.status_code, 200)
        self.assertIn(b"Cantidad de asistentes", respuesta.data)

    def test_pastor_no_puede_registrar_asistencia(self):
        self.iniciar_sesion_como("pastor")

        respuesta = self.cliente.get("/asistencia/nueva")

        self.assertEqual(respuesta.status_code, 403)

    def test_no_se_acepta_cantidad_negativa_de_asistentes(self):
        self.iniciar_sesion_como("secretario")

        respuesta = self.cliente.post(
            "/asistencia/nueva",
            data={"fecha": "2026-10-16", "tipo": "culto", "cantidad": "-1"},
        )

        self.assertEqual(respuesta.status_code, 200)
        self.assertIn("entre 0 y 5000".encode(), respuesta.data)

    def test_tesorero_puede_abrir_informes(self):
        self.iniciar_sesion_como("tesorero")

        respuesta = self.cliente.get("/informes")

        self.assertEqual(respuesta.status_code, 200)
        self.assertIn(b"Totales por categor", respuesta.data)

    def test_secretario_no_puede_consultar_informes_economicos(self):
        self.iniciar_sesion_como("secretario")

        respuesta = self.cliente.get("/informes")

        self.assertEqual(respuesta.status_code, 403)

    def test_periodo_invertido_en_informe_se_rechaza(self):
        self.iniciar_sesion_como("administrador")

        respuesta = self.cliente.get("/informes?desde=2026-10-20&hasta=2026-10-01")

        self.assertEqual(respuesta.status_code, 200)
        self.assertIn("fecha inicial no puede ser posterior".encode(), respuesta.data)

    def test_administrador_puede_abrir_gestion_de_usuarios(self):
        self.iniciar_sesion_como("administrador")

        respuesta = self.cliente.get("/admin/usuarios")

        self.assertEqual(respuesta.status_code, 200)
        self.assertIn(b"Usuarios y roles", respuesta.data)

    def test_secreto_insuficiente_impide_arranque_produccion(self):
        with self.assertRaisesRegex(RuntimeError, "SECRET_KEY"):
            iglesia_app._validar_configuracion({
                "SECRET_KEY": "clave-corta",
                "FLASK_ENV": "production",
                "MONGO_URI": "mongodb+srv://atlas.example/iglesia",
            })

    def test_base_local_se_rechaza_en_produccion(self):
        with self.assertRaisesRegex(RuntimeError, "MONGO_URI"):
            iglesia_app._validar_configuracion({
                "SECRET_KEY": "k" * 48,
                "FLASK_ENV": "production",
                "MONGO_URI": "mongodb://localhost:27017/iglesia",
            })

    def test_tesorero_no_puede_gestionar_usuarios(self):
        self.iniciar_sesion_como("tesorero")

        respuesta = self.cliente.get("/admin/usuarios")

        self.assertEqual(respuesta.status_code, 403)

    def test_usuario_nuevo_exige_contrasena_segura(self):
        self.iniciar_sesion_como("administrador")

        respuesta = self.cliente.post(
            "/admin/usuarios/nuevo",
            data={"nombre": "Secretaria", "usuario": "secretaria", "rol": "secretario", "contrasena": "corta"},
        )

        self.assertEqual(respuesta.status_code, 200)
        self.assertIn("al menos 12 caracteres".encode(), respuesta.data)

    def test_usuario_nuevo_rechaza_contrasena_demasiado_larga(self):
        self.iniciar_sesion_como("administrador")

        respuesta = self.cliente.post(
            "/admin/usuarios/nuevo",
            data={
                "nombre": "Secretaria",
                "usuario": "secretaria",
                "rol": "secretario",
                "contrasena": "a" * 73,
            },
        )

        self.assertEqual(respuesta.status_code, 200)
        self.assertIn("72 bytes UTF-8".encode(), respuesta.data)

    def test_pastor_no_puede_crear_movimientos(self):
        self.iniciar_sesion_como("pastor")

        respuesta = self.cliente.post("/economia/nuevo", data={})

        self.assertEqual(respuesta.status_code, 403)

    def test_monto_no_positivo_se_rechaza_sin_escribir_en_mongo(self):
        self.iniciar_sesion_como("tesorero")

        respuesta = self.cliente.post(
            "/economia/nuevo",
            data={
                "tipo": "ingreso",
                "categoria": "Ofrendas",
                "descripcion": "Prueba de validación",
                "monto": "0",
                "fecha": "2026-10-13",
            },
        )

        self.assertEqual(respuesta.status_code, 200)
        self.assertIn("mayor que cero".encode(), respuesta.data)

    def test_ruta_de_historial_solo_es_para_administrador(self):
        self.iniciar_sesion_como("administrador")

        respuesta = self.cliente.get("/admin/historial")

        # La vista presenta su estado vacío si MongoDB no está configurado.
        self.assertEqual(respuesta.status_code, 200)
        self.assertIn(b"Historial de cambios", respuesta.data)

    def test_secretario_puede_abrir_formulario_de_miembro(self):
        self.iniciar_sesion_como("secretario")

        respuesta = self.cliente.get("/miembros/nuevo")

        self.assertEqual(respuesta.status_code, 200)
        self.assertIn(b"Nombre completo", respuesta.data)

    def test_pastor_no_puede_abrir_formulario_de_miembro(self):
        self.iniciar_sesion_como("pastor")

        respuesta = self.cliente.get("/miembros/nuevo")

        self.assertEqual(respuesta.status_code, 403)

    def test_no_se_guarda_miembro_sin_nombre(self):
        self.iniciar_sesion_como("administrador")

        respuesta = self.cliente.post("/miembros/nuevo", data={"nombre": "   "})

        self.assertEqual(respuesta.status_code, 200)
        self.assertIn("nombre es obligatorio".encode(), respuesta.data)

    def test_listado_de_miembros_admite_busqueda(self):
        self.iniciar_sesion_como("administrador")

        respuesta = self.cliente.get("/miembros?q=Ana")

        self.assertEqual(respuesta.status_code, 200)
        self.assertIn(b"Buscar miembro", respuesta.data)
        self.assertIn(b"value=\"Ana\"", respuesta.data)

if __name__ == "__main__":
    unittest.main()
