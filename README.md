# Gestión de la Iglesia Metodista

Aplicación web móvil primero para registrar la economía, asistencia y miembros de la iglesia. Está construida con Flask, Jinja, Bootstrap y MongoDB Atlas.

## Requisitos

- Python 3.11 o superior
- Una instancia MongoDB Atlas

## Preparación local

1. Crea el entorno e instala dependencias:

   ```powershell
   python -m venv venv
   .\venv\Scripts\Activate.ps1
   pip install -r requirements.txt
   ```

2. Copia `.env.example` a `.env` y configura `MONGO_URI`, `MONGO_DB_NAME` y un `SECRET_KEY` aleatorio y privado.
3. Crea el primer usuario administrador:

   ```powershell
   python crear_admin.py
   ```

4. Inicia la aplicación:

   ```powershell
   flask --app app run --debug
   ```

   Abre http://127.0.0.1:5000 en el navegador.

## Pruebas de rutas

Con el entorno virtual activado, ejecuta las pruebas automáticas con:

```powershell
.\venv\Scripts\python.exe -m unittest -v test_routes
```

Estas pruebas usan el cliente de pruebas de Flask: simulan solicitudes HTTP sin arrancar un servidor y ponen la conexión a MongoDB en modo desconectado para evitar tocar datos reales. Comprueban que el login y el dashboard se muestran, que las páginas privadas redirigen a quien no inició sesión, que los roles respetan permisos y que se rechazan montos inválidos.

También hay pruebas de integración para la gestión de usuarios. Para ejecutarlas contra una instancia de prueba (local o desechable), define `MONGO_TEST_URI` solo en esa terminal:

```powershell
$env:MONGO_TEST_URI = 'mongodb://localhost:27017'
.\.venv312\Scripts\python.exe -m unittest -v test_integration_usuarios
Remove-Item Env:MONGO_TEST_URI
```

Las pruebas crean y borran una base con nombre aleatorio `iglesia_test_usuarios_...`; no usan la base `iglesia`. No pongas la URI productiva en `MONGO_TEST_URI`.

Si falla una prueba, el nombre indica el comportamiento que no se cumple; la salida muestra el código de respuesta esperado y el obtenido. Estas pruebas no sustituyen las pruebas de integración con una base MongoDB de prueba, que harán falta para verificar escrituras, filtros y auditoría.

Si Python apunta a una versión alpha y aparece un error al importar `markupsafe._speedups`, crea otro entorno con una versión estable de Python (por ejemplo, 3.12), instala las dependencias allí y ejecuta las pruebas con ese intérprete:

```powershell
py -3.12 -m venv .venv312
.\.venv312\Scripts\python.exe -m pip install -r requirements.txt
.\.venv312\Scripts\python.exe -m unittest -v test_routes
```

## Despliegue en Render

La plantilla [render.yaml](render.yaml) configura el servicio web, Gunicorn y la comprobación de salud. Para desplegar:

1. Crea una base de datos en MongoDB Atlas y un usuario con acceso solo a esa base.
2. En Atlas, permite el acceso de red desde Render. Para aprender/pruebas se puede usar `0.0.0.0/0`, pero deja la base protegida con credenciales fuertes y permisos mínimos; en producción restringe por IP de salida cuando tu plan de Render lo permita.
3. Sube el proyecto a un repositorio Git privado **sin `.env`** y crea el Web Service de Render desde ese repo (o usa el Blueprint `render.yaml`).
4. En las variables del servicio, define `MONGO_URI` con la cadena real de Atlas. Render generará `SECRET_KEY`; deja `FLASK_ENV=production` y `MONGO_DB_NAME=iglesia`.
5. El endpoint `/health` de Render comprueba conexión a MongoDB. Espera el deploy y abre la URL de Render. La app falla al iniciar en producción si falta una clave fuerte o si `MONGO_URI` apunta a localhost.
6. Crea el primer administrador con una conexión a Atlas desde tu máquina: coloca temporalmente la URI de Atlas en `.env`, ejecuta `crear_admin.py` y luego no subas ni compartas ese archivo.

El archivo de `.env` local contiene un secreto distinto y solo sirve para desarrollo. No compartas capturas con URI, contraseña ni valores de `SECRET_KEY`.

## Copias de seguridad

- Atlas: revisa las opciones de backup disponibles para el nivel contratado; no todos los niveles gratuitos incluyen snapshots automáticos.
- MongoDB local: instala MongoDB Database Tools y ejecuta `./backup_mongodb.ps1`. El script crea `backups/<base>_<fecha>.archive.gz`; el directorio queda excluido de Git.
- Guarda copias fuera de la laptop, cifradas y con acceso limitado. Una copia que nunca se ha restaurado no está verificada. Para restaurar, sigue la documentación de `mongorestore` y confirma el destino antes de cualquier opción `--drop`, porque reemplaza datos.
- Para comprobar una copia sin tocar la base original, restaura primero a otra base local con `--nsFrom="iglesia.*" --nsTo="iglesia_restaurada.*"`; revisa los conteos y solo después considera una restauración real. Nunca apuntes la prueba al nombre `iglesia` ni uses `--drop` allí.

## Estado de los módulos

- Inicio: resumen mensual, últimos movimientos y asistencia de los últimos domingos.
- Economía: lista, filtros, creación, edición y anulación con motivo y auditoría.
- Miembros: alta, búsqueda por nombre/contacto/grupo, edición y archivado lógico con auditoría; solo administrador y secretario pueden modificar registros.
- Asistencia: registro, filtro por mes/actividad, edición y archivado no destructivo con auditoría; alimenta la gráfica del panel.
- Informes: resumen económico por fechas con totales y detalle por categoría, descargable en Excel y PDF.
- Historial: consulta de eventos recientes; incluye los cambios de economía y miembros.
- Usuarios: el primer administrador se crea con `crear_admin.py`; el administrador puede crear cuentas, asignar/editar roles, cambiar contraseñas y activar/desactivar usuarios desde la app. Se protege la última cuenta administradora activa.
