# API RUC Ecuador

MVP para consulta de RUC con procedencia por campo. El SRI es la autoridad para el estado tributario. La Superintendencia de Compañías, Valores y Seguros (SCVS) añade la situación legal societaria de las compañías que consten en su directorio. Son datos distintos y la API no los mezcla.

## Fuentes

- **SRI en vivo:** [consulta pública del contribuyente](https://srienlinea.sri.gob.ec/facturacion-internet/consultas/publico/ruc-datos2.jspa). El adaptador lee el HTML público y falla con claridad si el portal cambia o no responde.
- **Snapshot SRI:** [datasets SRI](https://www.sri.gob.ec/datasets), archivo anual de contribuyentes activos. Se usa como respaldo fechado, nunca como estado en tiempo real.
- **Directorio SCVS:** [reporte oficial de compañías](https://mercadodevalores.supercias.gob.ec/reportes/directorioCompanias.jsf), con actualización reportada por la misma página. El directorio excluye algunas compañías canceladas; la ausencia de una fila no prueba inexistencia.

El [catálogo de Datos Abiertos Ecuador](https://www.datosabiertos.gob.ec/dataset/?res_format=CSV&tags=RUC) sirve para descubrir y distribuir conjuntos, pero no se cuenta como corroboración separada cuando republica un archivo del SRI.

## Arranque

Requiere Python 3.11 o posterior.

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
uvicorn app.main:app --reload
```

Documentación interactiva: `http://127.0.0.1:8000/docs`.

## Verificación y GitHub

Instala las dependencias de desarrollo y ejecuta las pruebas antes de proponer cambios:

```powershell
python -m pip install -r requirements-dev.txt
python -m pytest
```

GitHub Actions ejecuta estas pruebas en las solicitudes de incorporación a `main` y en los cambios enviados a esa rama. Usa ramas de trabajo y solicitudes de incorporación; antes del primer commit, comprueba que `.env`, `.venv/` y `data/` no aparecen entre los archivos preparados. `.env.example` contiene únicamente valores de ejemplo.

En esta primera etapa, `main` es la única rama y será la línea de producción cuando la aplicación se publique. Todavía no se crean ramas, entornos de GitHub ni despliegues separados de QA o Producción; se añadirán después de que la aplicación esté publicada desde `main`. La verificación CI no equivale a un entorno QA. Al crear el repositorio, configura protección de `main` para exigir solicitudes de incorporación y que pase la comprobación CI.

## Rutas

- `GET /healthz`
- `GET /api/v1/sources`
- `GET /api/v1/ruc/{ruc}`

Ejemplo: `GET /api/v1/ruc/1790013731001`

La respuesta incluye procedencia, hora de observación, fecha de actualización conocida y frescura para cada campo. Una diferencia de razón social se devuelve como conflicto visible; no se escoge un valor silenciosamente. Un fallo del SRI activa el snapshot local si existe.

## Sincronizar snapshots

```powershell
python scripts/sync_sources.py --only sri
python scripts/sync_sources.py --only scvs
python scripts/sync_sources.py
```

El sincronizador descarga el archivo anual de activos publicado en SRI Datasets y el Excel del directorio SCVS, y sustituye cada snapshot solo después de reconocer e importar filas. Si el archivo cambia su estructura, la carga falla y conserva los datos previamente importados. La descarga SCVS es de aproximadamente 35 MB; programe la sincronización una vez al día o con menor frecuencia, no por petición de API.

Por defecto, la base SQLite se guarda en `data/snapshots.sqlite3`. `DATABASE_PATH` puede apuntar a otro archivo. Configure `SRI_DATASET_YEAR` cuando cambie el archivo anual que quiera importar.

## Límites del MVP

- La consulta en vivo del SRI es una página pública, no una API contractual documentada; un cambio de HTML puede hacer que el adaptador falle cerrado.
- La descarga anual del SRI contiene una fotografía. La API devuelve ese período cuando lo usa como respaldo.
- El directorio SCVS representa solo su ámbito de competencia y omite ciertas compañías canceladas.
- Se valida la forma de 13 dígitos; no se rechazan números por algoritmo de dígito verificador, porque existen categorías de RUC del SRI sin ese algoritmo.
- Este primer alcance cubre perfil público de RUC; no consulta declaraciones, valores de facturas, deudas privadas ni credenciales SRI.
