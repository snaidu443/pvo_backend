"""FastAPI application: routers, request-size limit, security headers and safe error responses.

Run locally:  uvicorn app.main:app --reload
"""
import logging
import uuid

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .config import get_settings
from .routers import activity, assessment, assets, auth, catalog, documents, extraction, health, inputs, structx, structx_users, users

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s %(message)s')
log = logging.getLogger('pvo')


def create_app() -> FastAPI:
    settings = get_settings()
    settings.check()
    docs = settings.docs_enabled
    app = FastAPI(title='PVO API', version='0.1.0', docs_url='/docs' if docs else None,
                  redoc_url=None, openapi_url='/openapi.json' if docs else None)

    if settings.cors_origins:  # only when the React app is on another domain; exact origins, never '*'
        app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins, allow_credentials=True,
                           allow_methods=['GET', 'POST', 'PUT', 'PATCH', 'DELETE'], allow_headers=['Authorization', 'Content-Type'])

    @app.middleware('http')
    async def guard(request: Request, call_next):
        request_id = uuid.uuid4().hex[:12]
        request.state.request_id = request_id
        if request.method in ('POST', 'PUT', 'PATCH'):
            limit = settings.max_upload_bytes + 1024 * 1024 if request.url.path.endswith('/documents') else settings.max_json_bytes
            length = request.headers.get('content-length')
            if length is None and request.headers.get('transfer-encoding'):
                return JSONResponse({'detail': 'Content-Length is required.'}, status_code=411)
            if length is not None and (not length.isdigit() or int(length) > limit):
                return JSONResponse({'detail': 'The request is too large.'}, status_code=413)
        response = await call_next(request)
        response.headers['X-Request-ID'] = request_id
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers.setdefault('Cache-Control', 'no-store')
        return response

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, exc: RequestValidationError):
        # Say what is wrong and where, without echoing the submitted values back.
        errors = [{'loc': list(e.get('loc', [])), 'msg': e.get('msg', '')} for e in exc.errors()[:20]]
        return JSONResponse({'detail': 'Some values are missing or not valid.', 'errors': errors}, status_code=422)

    @app.exception_handler(Exception)
    async def unexpected(request: Request, exc: Exception):
        log.exception('Unhandled error (request %s)', getattr(request.state, 'request_id', '-'))
        return JSONResponse({'detail': 'Something went wrong. Please try again.'}, status_code=500)

    app.include_router(health.router)
    for module in (auth, catalog, assets, documents, extraction, inputs, assessment, users, activity, structx, structx_users):
        app.include_router(module.router, prefix=settings.api_prefix)
    if settings.dev_login:  # config.check() has already refused this outside local
        from . import devauth
        app.include_router(devauth.router, prefix=settings.api_prefix)
        log.warning('LOCAL DEV SIGN-IN IS ON (POST %s/auth/dev-login). Okta tokens are not accepted.', settings.api_prefix)
    return app


app = create_app()
