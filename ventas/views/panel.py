from django.views.generic import ListView, DeleteView, UpdateView
from django.utils.decorators import method_decorator
from django.contrib.admin.views.decorators import staff_member_required
from django.urls import reverse_lazy
from django.views.generic import CreateView, DetailView
from ventas.models import Pedido
from ..views.helpers import descontar_stock
from django.contrib import messages
from django.db.models import Sum, Count, F, DecimalField, ExpressionWrapper, Q
from django.views.generic import TemplateView
from django.utils import timezone
from datetime import timedelta
import json
from django.http import JsonResponse
from django.db import transaction
from django.views.generic import ListView, DeleteView, UpdateView, CreateView, DetailView, TemplateView, View
from django.template.loader import render_to_string
from productos.models import Producto
from ventas.models import Pedido, DetallePedido
from ventas.views.helpers import registrar_historial, registrar_log
@method_decorator(staff_member_required, name='dispatch')
class ReportesVentasView(TemplateView):
    template_name = 'ventas/reportes.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        hoy = timezone.now()
        hace_30_dias = hoy - timedelta(days=30)

        # 1. Filtramos las ventas válidas (Cabeceras)
        ventas_validas = Pedido.objects.filter(
            fecha_pedido__gte=hace_30_dias,
            estado__in=['pagado', 'entregado']
        )

        # 2. Recaudación total: Ahora es MUCHO más rápido porque sumamos directamente el campo 'total' del Pedido
        metricas = ventas_validas.aggregate(
            recaudacion_total=Sum('total'),
            conteo=Count('id')
        )

        total_recaudado = metricas['recaudacion_total'] or 0
        cantidad_ventas = metricas['conteo'] or 0
        ticket_promedio = total_recaudado / cantidad_ventas if cantidad_ventas > 0 else 0

        # 3. Productos más vendidos (Top 5): Ahora tenemos que preguntarle a la tabla de Detalles!
        top_productos = (
            DetallePedido.objects.filter(pedido__estado__in=['pagado', 'entregado'])
            .values('producto__nombre')
            .annotate(total_vendido=Sum('cantidad'))
            .order_by('-total_vendido')[:5]
        )

        # 4. Ventas por método de pago (Esto queda igual porque el método de pago está en la cabecera)
        metodos_pago = (
            ventas_validas
            .values('metodo_pago')
            .annotate(count=Count('id'))
        )

        context.update({
            'total_recaudado': total_recaudado,
            'cantidad_ventas': cantidad_ventas,
            'ticket_promedio': ticket_promedio,
            'top_productos': top_productos,
            'metodos_pago': metodos_pago,
            'rango': "Últimos 30 días"
        })
        return context

@method_decorator(staff_member_required, name='dispatch')
class VentaMostradorView(TemplateView):
    template_name = 'ventas/venta_mostrador.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        
        # Obtenemos productos con stock
        productos = Producto.objects.filter(stock__gt=0)
        
        # Formateamos el catálogo en una lista de diccionarios (Data pura)
        catalogo = [
            {
                'id': p.id,
                'nombre': p.nombre,
                'precio': float(p.precio_display), # Convertimos a float para JS
                'stock': p.stock
            }
            for p in productos
        ]
        
        # Pasamos el catálogo al contexto
        context['catalogo_json'] = catalogo
        return context

    def post(self, request, *args, **kwargs):
        """
        Recibe el "ticket" armado en JavaScript como un paquete JSON
        y guarda la cabecera y sus múltiples detalles.
        """
        try:
            datos = json.loads(request.body)
            items = datos.get('items', [])
            metodo_pago = datos.get('metodo_pago', 'efectivo')

            if not items:
                return JsonResponse({'error': 'El carrito está vacío'}, status=400)

            # transaction.atomic() asegura que si falla el producto 5, 
            # no se guarde ni se descuente el stock de los 4 anteriores.
            with transaction.atomic():
                # 1. Creamos la cabecera (Ticket)
                pedido = Pedido.objects.create(
                    usuario=request.user,  # El cajero queda registrado como dueño
                    metodo_pago=metodo_pago,
                    estado='entregado',    # Venta de mostrador se entrega en el acto
                    total=0
                )
                
                total_calculado = 0

                # 2. Iteramos los renglones del ticket
                for item in items:
                    # select_for_update() bloquea la fila en la DB temporalmente para evitar 
                    # que otra persona compre el mismo producto en el mismo milisegundo
                    producto = Producto.objects.select_for_update().get(id=item['id'])
                    cantidad = int(item['cantidad'])

                    if producto.stock < cantidad:
                        raise ValueError(f"Stock insuficiente para {producto.nombre}")

                    # Descontamos el stock de la base de datos
                    producto.stock -= cantidad
                    producto.save()

                    # Guardamos el renglón
                    DetallePedido.objects.create(
                        pedido=pedido,
                        producto=producto,
                        cantidad=cantidad,
                        precio_unitario=producto.precio_display
                    )
                    total_calculado += (producto.precio_display * cantidad)

                # 3. Cerramos el total de la cabecera
                pedido.total = total_calculado
                pedido.save()

                # Registramos en tus logs
                registrar_historial(pedido, "", "entregado", request.user)
                registrar_log(pedido, request.user, "Venta mostrador (POS múltiple)")

            # Respuesta exitosa para que JavaScript imprima el comprobante
            return JsonResponse({'success': True, 'pedido_id': pedido.id})

        except ValueError as e:
            return JsonResponse({'error': str(e)}, status=400)
        except Exception as e:
            return JsonResponse({'error': 'Error interno al procesar la venta'}, status=500)
    
@method_decorator(staff_member_required, name='dispatch')
class PanelPedidosView(ListView):
    model = Pedido
    template_name = 'ventas/panel_pedidos.html'
    context_object_name = 'pedidos'

    def get_queryset(self):
        # CAMBIO CLAVE: Cambiamos select_related por prefetch_related para traer los detalles sin ahorcar la base de datos
        qs = Pedido.objects.select_related('usuario').prefetch_related('detalles__producto').all()

        query = self.request.GET.get('q') 
        estado = self.request.GET.get('estado')
        producto = self.request.GET.get('producto')
        usuario = self.request.GET.get('usuario')
        fecha = self.request.GET.get('fecha')

        if query:
            clean_query = query.replace('#', '')
            # Buscamos el nombre del producto DENTRO de los detalles. Usamos distinct() para no duplicar el pedido si tiene varios productos con "cepillo".
            qs = qs.filter(
                Q(detalles__producto__nombre__icontains=query) | 
                Q(id__icontains=clean_query)
            ).distinct()

        # Mantenemos los filtros
        if estado:
            qs = qs.filter(estado=estado)
        if producto:
            qs = qs.filter(detalles__producto__nombre__icontains=producto).distinct()
        if usuario:
            qs = qs.filter(usuario__username__icontains=usuario)
        if fecha:
            qs = qs.filter(fecha_pedido__date=fecha)

        return qs.order_by('-fecha_pedido')

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        
        # Obtenemos el queryset filtrado (búsqueda y filtros)
        qs = self.get_queryset()

        # Separamos en dos listas (Activos/Pendientes vs Finalizados)
        # QA Fix: 'pagado' es activo porque el dueño aún debe enviarlo/entregarlo.
        estados_activos = ['pendiente', 'pendiente_transferencia', 'pagado', 'enviado']
        estados_terminados = ['entregado', 'cancelado']

        # Si no hay filtros aplicados, limitamos a los últimos 30
        if not self.request.GET:
            context['pedidos_activos'] = qs.filter(estado__in=estados_activos)[:30]
            context['pedidos_terminados'] = qs.filter(estado__in=estados_terminados)[:30]
        else:
            # QA Fix: Límite de seguridad en búsquedas para no cargar 10.000 registros en RAM
            context['pedidos_activos'] = qs.filter(estado__in=estados_activos)[:50]
            context['pedidos_terminados'] = qs.filter(estado__in=estados_terminados)[:50]

        # Ya no usamos object_list / pedidos general
        context['pedidos'] = None 

        # Definición de estados para el negocio
        estados_lista = ['pendiente', 'pagado', 'enviado', 'entregado', 'cancelado']

        # Estadísticas globales (usando la lista para consistencia)
        # Optimizamos contando sobre el modelo directamente
        estadisticas = {
            est: Pedido.objects.filter(estado=est).count()
            for est in estados_lista
        }

        context.update({
            'total_pedidos': Pedido.objects.count(),
            'estadisticas': estadisticas,
            'estados': estados_lista,
            
            # Variables de retorno para persistencia en el template
            'f_estado': self.request.GET.get('estado', ''),
            'f_producto': self.request.GET.get('producto', ''),
            'f_usuario': self.request.GET.get('usuario', ''),
            'f_fecha': self.request.GET.get('fecha', ''),
            'q': self.request.GET.get('q', ''), # Retornamos la búsqueda actual
        })
        return context


@method_decorator(staff_member_required, name='dispatch')
class PedidoDeleteView(DeleteView):
    """
    Eliminar un pedido desde el panel (solo staff).
    """
    model = Pedido
    template_name = 'pedidos/pedido_confirm_delete.html'
    success_url = reverse_lazy('panel:panel_pedidos')


@method_decorator(staff_member_required, name='dispatch')
class PedidoEntregarView(UpdateView):
    """
    Confirmar entrega de un pedido desde el panel (solo staff).
    """
    model = Pedido
    fields = []  # no mostramos formulario
    template_name = 'pedidos/pedido_entregar.html'
    success_url = reverse_lazy('panel:panel_pedidos')

    def form_valid(self, form):
        self.object.estado = 'entregado'
        self.object.save()
        return super().form_valid(form)
    
@method_decorator(staff_member_required, name='dispatch')
class TicketVentaDetailView(DetailView):
    model = Pedido
    template_name = 'ventas/ticket_pdf.html' 
    context_object_name = 'pedido'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        # El total ya no se calcula multiplicando porque ahora viene guardado en el pedido
        context['total'] = self.object.total
        return context

@method_decorator(staff_member_required, name='dispatch')
class PedidoHistorialModalView(View):
    """
    Devuelve un trozo de HTML (Partial) con el timeline unificado del pedido
    para inyectarlo en el Modal asíncrono del panel.
    """
    def get(self, request, pk, *args, **kwargs):
        try:
            pedido = Pedido.objects.get(pk=pk)
        except Pedido.DoesNotExist:
            return JsonResponse({'error': 'Pedido no encontrado'}, status=404)

        # 1. Obtener Historial de Estados
        historial = pedido.historial.all()
        # 2. Obtener Logs Generales
        logs = pedido.logs.all()

        # Unificar ambos en una sola lista de diccionarios para el timeline
        timeline = []
        for h in historial:
            timeline.append({
                'tipo': 'estado',
                'fecha': h.fecha_cambio,
                'descripcion': f"Estado cambiado a {h.estado_nuevo}",
                'estado_nuevo': h.estado_nuevo,
                'usuario': h.usuario.username if h.usuario else "Sistema"
            })
            
        for l in logs:
            timeline.append({
                'tipo': 'log',
                'fecha': l.fecha,
                'descripcion': l.accion,
                'usuario': l.usuario.username if l.usuario else "Sistema"
            })
            
        # Ordenar por fecha cronológica (el más reciente primero)
        timeline.sort(key=lambda x: x['fecha'], reverse=True)

        # Renderizar un template parcial
        html = render_to_string('ventas/partials/historial_timeline.html', {
            'pedido': pedido,
            'timeline': timeline
        })
        
        return JsonResponse({'html': html})

@method_decorator(staff_member_required, name='dispatch')
class GestorOfertasView(TemplateView):
    template_name = 'ventas/gestor_ofertas.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        # Separamos los productos para la vista
        context['kits_carrusel'] = Producto.objects.filter(en_carrusel=True).order_by('-en_oferta', 'nombre')
        context['productos_individuales'] = Producto.objects.filter(en_carrusel=False).order_by('-en_oferta', 'nombre')
        return context

    def post(self, request, *args, **kwargs):
        # 1. Manejo de bulk update vía JSON
        if request.headers.get('Content-Type') == 'application/json':
            try:
                data = json.loads(request.body)
                if data.get('action') == 'update_all':
                    productos_data = data.get('productos', [])
                    with transaction.atomic():
                        for p_data in productos_data:
                            producto = Producto.objects.get(id=p_data['id'])
                            producto.en_oferta = p_data.get('en_oferta', False)
                            producto.destacado = p_data.get('destacado', False)
                            producto.en_carrusel = p_data.get('en_carrusel', False)
                            producto.etiqueta_oferta = p_data.get('etiqueta_oferta', '')
                            
                            precio_str = p_data.get('precio_oferta', '')
                            if precio_str:
                                producto.precio_oferta = precio_str.replace(',', '.')
                            else:
                                producto.precio_oferta = None
                                
                            fecha_str = p_data.get('fecha_fin_oferta', '')
                            if fecha_str:
                                producto.fecha_fin_oferta = fecha_str
                            else:
                                producto.fecha_fin_oferta = None
                                
                            producto.save()
                    
                    messages.success(request, "¡Todas las ofertas actualizadas con éxito! 🚀 (Guardado Masivo)")
                    return JsonResponse({'success': True})
            except Exception as e:
                return JsonResponse({'success': False, 'error': str(e)})

        # Si la petición no es JSON (no debería pasar ahora que quitamos los forms), devolvemos error
        messages.error(request, "Método de guardado obsoleto. Por favor, usa el botón 'Guardar Todo'.")
        return self.get(request, *args, **kwargs)
        