from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import LoginView
from django.shortcuts import render
from django.views.decorators.http import require_GET

from .forms import FormularioEntrada
from .roles import es_responsable, solo_responsable


class Entrar(LoginView):
    template_name = "cuentas/entrar.html"
    authentication_form = FormularioEntrada
    redirect_authenticated_user = True


@login_required
@require_GET
def inicio(request):
    empleado = getattr(request.user, "empleado", None)
    return render(
        request,
        "cuentas/inicio.html",
        {"empleado": empleado, "responsable": es_responsable(request.user)},
    )


@solo_responsable
def panel_responsable(request):
    """Punto de entrada del responsable; la bandeja de solicitudes llega con US-018."""
    return render(request, "cuentas/panel_responsable.html", {"empleado": request.user.empleado})
