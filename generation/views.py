import os
from uuid import uuid4

import requests
from django.core.files.base import ContentFile
from decimal import Decimal

from django.db import transaction
from django.utils import timezone
from django.shortcuts import get_object_or_404
from rest_framework import status, serializers
from rest_framework.parsers import MultiPartParser, FormParser, JSONParser
from rest_framework.response import Response
from rest_framework.views import APIView

from decks.models import Deck, DeckCard
from decks.serializers import DeckCardSerializer, DeckDetailSerializer, deck_detail_context
from .errors import short_error
from .auto_references import fetch_auto_reference, fetch_auto_references
from .jobs import expire_stale_jobs

from .models import GeneratedImage, GenerationJob, ThemeAssignment, ReferenceImage
from .selection import batch_limit, pending_entries, assignment_map
from .theme_planner import build_theme_plan
from .theme_interview import build_theme_questions, deck_brief, validate_theme_answers
from .prompt_builder import make_cache_key
from .references import reference_hashes, normalize_reference, MAX_REFERENCE_BYTES
from .providers import get_provider
from .runner import render_card_image, save_card_image, start_job
from .serializers import GenerationJobSerializer, ThemeAssignmentInputSerializer


class GenerateDeckView(APIView):
    def post(self, request, deck_id):
        deck = get_object_or_404(Deck, pk=deck_id)
        expire_stale_jobs(deck)
        if deck.jobs.filter(status__in=["pending", "running"]).exists():
            return Response({"detail": "Ya hay un job activo para este mazo."}, status=status.HTTP_409_CONFLICT)
        limit = request.data.get("limit", batch_limit())
        if type(limit) is not int or limit < 1:
            return Response({"limit": ["Debe ser un entero mayor o igual a 1."]}, status=400)
        _, pending = pending_entries(deck)
        selected = pending[:min(limit, batch_limit())]
        if not selected:
            return Response({"detail": "No hay cartas pendientes de generar."}, status=400)
        job = GenerationJob.objects.create(deck=deck, total_cards=len(selected),
                                          card_oracle_ids=[str(row.card.oracle_id) for row in selected])
        start_job(job)
        return Response(GenerationJobSerializer(job).data, status=status.HTTP_202_ACCEPTED)


class JobDetailView(APIView):
    def get(self, request, job_id):
        return Response(GenerationJobSerializer(get_object_or_404(GenerationJob, pk=job_id)).data)


class RegenerateCardView(APIView):
    def post(self, request, card_id):
        deck_card = get_object_or_404(DeckCard.objects.select_related("card", "deck"), pk=card_id)
        if not deck_card.card:
            return Response({"detail": "La carta no fue resuelta."}, status=status.HTTP_400_BAD_REQUEST)
        expire_stale_jobs(deck_card.deck)
        if deck_card.deck.jobs.filter(status__in=["pending", "running"]).exists():
            return Response({"detail": "Hay una generación en curso para este mazo."}, status=status.HTTP_409_CONFLICT)
        brief = deck_brief(deck_card.deck)
        assignments = assignment_map(deck_card.deck)
        assignment = assignments.get(deck_card.card.oracle_id)
        theme = deck_card.deck.theme
        try:
            provider = get_provider()
            key = make_cache_key(deck_card.card.oracle_id, theme, provider.name, assignment, brief, deck_card.deck.card_layout, reference_hashes(assignment))
            prompt_json, image_bytes = render_card_image(deck_card.card, theme, provider, assignment, brief, deck_card.deck.card_layout)
            with transaction.atomic():
                old = GeneratedImage.objects.select_for_update().filter(cache_key=key).first()
                old_file = old.image if old else None
                if old:
                    old.delete()
                generated = save_card_image(deck_card.card, theme, provider, prompt_json, image_bytes, assignment, brief, deck_card.deck.card_layout)
                if old_file:
                    transaction.on_commit(lambda: old_file.delete(save=False))
        except Exception as exc:
            return Response({"detail": short_error(exc)}, status=status.HTTP_502_BAD_GATEWAY)
        return Response(DeckCardSerializer(deck_card, context={"request": request, "generated_images": {key: generated}, "assignments": assignments, "brief": brief}).data)


class DeckEstimateView(APIView):
    def get(self, request, deck_id):
        deck = get_object_or_404(Deck, pk=deck_id)
        entries, pending = pending_entries(deck)
        to_generate = len(pending)
        price = Decimal(os.getenv("COST_PER_IMAGE_USD", "0.04"))
        size = min(batch_limit(), to_generate)
        return Response({
            "unique_cards": len(entries), "cached": len(entries) - to_generate, "to_generate": to_generate,
            "estimated_cost_usd": float((price * to_generate).quantize(Decimal("0.01"))),
            "batch_limit": batch_limit(), "batch_size": size,
            "batch_cost_usd": float((price * size).quantize(Decimal("0.01"))),
        })


class ThemePlanView(APIView):
    def post(self, request, deck_id):
        deck = get_object_or_404(Deck, pk=deck_id)
        expire_stale_jobs(deck)
        if deck.jobs.filter(status__in=["pending", "running"]).exists():
            return Response({"detail": "Hay una generación en curso para este mazo."}, status=409)
        overwrite = request.data.get("overwrite_edited", False)
        if type(overwrite) is not bool:
            return Response({"overwrite_edited": ["Debe ser un booleano."]}, status=400)
        brief = deck_brief(deck)
        try:
            build_theme_plan(deck, overwrite_edited=overwrite, brief=brief)
        except Exception as exc:
            return Response({"detail": short_error(exc)}, status=502)
        deck = Deck.objects.prefetch_related("cards__card").get(pk=deck.pk)
        return Response(DeckDetailSerializer(deck, context=deck_detail_context(deck, request, brief=brief)).data)


class ThemeAssignmentView(APIView):
    def patch(self, request, card_id):
        entry = get_object_or_404(DeckCard.objects.select_related("card", "deck"), pk=card_id)
        if not entry.card_id:
            return Response({"detail": "La carta no fue resuelta."}, status=400)
        expire_stale_jobs(entry.deck)
        if entry.deck.jobs.filter(status__in=["pending", "running"]).exists():
            return Response({"detail": "Hay una generación en curso para este mazo."}, status=409)
        serializer = ThemeAssignmentInputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        with transaction.atomic():
            assignment, _ = ThemeAssignment.objects.get_or_create(deck=entry.deck, oracle_id=entry.card.oracle_id,
                                                                  defaults=serializer.validated_data)
            changed = any(field in serializer.validated_data and serializer.validated_data[field] != getattr(assignment, field)
                          for field in ("reference_wiki", "reference_page"))
            if changed:
                assignment.auto_reference_status = ''
                assignment.auto_reference_message = ''
                assignment.references.filter(origin='auto').delete()
            for field, value in serializer.validated_data.items():
                setattr(assignment, field, value)
            assignment.edited = True
            assignment.save()
        context = deck_detail_context(entry.deck, request)
        return Response(DeckCardSerializer(entry, context=context).data)


class ThemeQuestionsView(APIView):
    def post(self, request, deck_id):
        deck = get_object_or_404(Deck, pk=deck_id)
        expire_stale_jobs(deck)
        if deck.jobs.filter(status__in=["pending", "running"]).exists():
            return Response({"detail": "Hay una generación en curso para este mazo."}, status=409)
        try:
            questions = build_theme_questions(deck)
        except Exception as exc:
            return Response({"detail": short_error(exc)}, status=502)
        deck.theme_questions = questions
        deck.theme_answers = {}
        deck.theme_answers_updated_at = None
        deck.save(update_fields=["theme_questions", "theme_answers", "theme_answers_updated_at"])
        return Response(DeckDetailSerializer(deck, context=deck_detail_context(deck, request)).data)


class ThemeAnswersView(APIView):
    def put(self, request, deck_id):
        deck = get_object_or_404(Deck, pk=deck_id)
        expire_stale_jobs(deck)
        if deck.jobs.filter(status__in=["pending", "running"]).exists():
            return Response({"detail": "Hay una generación en curso para este mazo."}, status=409)
        try:
            answers = validate_theme_answers(deck, request.data.get("answers"))
        except ValueError as exc:
            return Response({"answers": [str(exc)]}, status=400)
        deck.theme_answers = answers
        deck.theme_answers_updated_at = timezone.now()
        deck.save(update_fields=["theme_answers", "theme_answers_updated_at"])
        return Response(DeckDetailSerializer(deck, context=deck_detail_context(deck, request)).data)


class AssignmentReferencesView(APIView):
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def _entry(self, card_id):
        return get_object_or_404(DeckCard.objects.select_related('card', 'deck'), pk=card_id)

    def _active(self, entry):
        expire_stale_jobs(entry.deck)
        return entry.deck.jobs.filter(status__in=['pending', 'running']).exists()

    def _response(self, entry, status=200):
        return Response(DeckCardSerializer(entry, context=deck_detail_context(entry.deck, self.request)).data, status=status)

    def post(self, request, card_id):
        entry = self._entry(card_id)
        if self._active(entry):
            return Response({'detail': 'Hay una generación en curso para este mazo.'}, status=409)
        assignment = entry.deck.theme_assignments.filter(oracle_id=entry.card.oracle_id).first() if entry.card_id else None
        if assignment is None:
            return Response({'detail': 'Primero define o genera el plan temático de esta carta.'}, status=400)
        if assignment.references.count() >= 3:
            return Response({'detail': 'Máximo 3 imágenes de referencia por carta.'}, status=400)
        if not hasattr(request.data, 'get') or len(request.FILES.getlist('file')) > 1:
            return Response({'detail': 'Envía un archivo o una URL de imagen.'}, status=400)
        file = request.FILES.get('file')
        url = request.data.get('url')
        if bool(file) == bool(url):
            return Response({'detail': 'Envía un archivo o una URL de imagen.'}, status=400)
        source_url = ''
        try:
            if file:
                raw = file.read(MAX_REFERENCE_BYTES + 1)
            else:
                try:
                    source_url = serializers.URLField(max_length=1000).run_validation(url)
                except serializers.ValidationError:
                    return Response({'detail': 'URL de imagen inválida.'}, status=400)
                response = requests.get(source_url, timeout=20, headers={'User-Agent': 'ProxyForge/1.0'})
                response.raise_for_status()
                if not response.headers.get('Content-Type', '').lower().startswith('image/'):
                    return Response({'detail': 'La URL no devuelve una imagen.'}, status=400)
                raw = response.content
            image_bytes, digest = normalize_reference(raw)
        except requests.RequestException:
            return Response({'detail': 'No se pudo descargar la imagen de referencia.'}, status=400)
        except ValueError as exc:
            return Response({'detail': str(exc)}, status=400)
        reference = None
        try:
            with transaction.atomic():
                assignment = ThemeAssignment.objects.select_for_update().get(pk=assignment.pk)
                existing = list(assignment.references.all())
                if len(existing) >= 3:
                    return Response({'detail': 'Máximo 3 imágenes de referencia por carta.'}, status=400)
                if any(item.sha256 == digest for item in existing):
                    return Response({'detail': 'Esa imagen ya está agregada.'}, status=400)
                reference = ReferenceImage(assignment=assignment, source_url=source_url, sha256=digest,
                                           position=max((item.position for item in existing), default=-1) + 1)
                reference.image.save(f'{uuid4().hex}.jpg', ContentFile(image_bytes), save=False)
                reference.save()
                assignment.edited = True
                assignment.save(update_fields=['edited', 'updated_at'])
        except Exception:
            if reference is not None and reference.image:
                reference.image.delete(save=False)
            raise
        return self._response(entry, status=201)

    def delete(self, request, card_id, reference_id):
        entry = self._entry(card_id)
        reference = get_object_or_404(ReferenceImage, pk=reference_id, assignment__deck=entry.deck,
                                      assignment__oracle_id=entry.card.oracle_id if entry.card_id else None)
        if self._active(entry):
            return Response({'detail': 'Hay una generación en curso para este mazo.'}, status=409)
        reference.delete()
        return self._response(entry)


class AutoReferencesView(APIView):
    def post(self, request, deck_id):
        deck = get_object_or_404(Deck, pk=deck_id)
        expire_stale_jobs(deck)
        if deck.jobs.filter(status__in=['pending', 'running']).exists():
            return Response({'detail': 'Hay una generación en curso para este mazo.'}, status=409)
        force = request.data.get('force', False)
        ids = request.data.get('deck_card_ids')
        if type(force) is not bool or (ids is not None and (not isinstance(ids, list) or
                any(type(value) is not int or value < 1 for value in ids))):
            return Response({'detail': 'force debe ser booleano y deck_card_ids una lista de ids.'}, status=400)
        summary = fetch_auto_references(deck, deck_card_ids=ids, force=force)
        data = DeckDetailSerializer(deck, context=deck_detail_context(deck, request)).data
        return Response({**data, 'auto_reference_summary': summary})


class AutoReferenceView(APIView):
    def post(self, request, card_id):
        entry = get_object_or_404(DeckCard.objects.select_related('card', 'deck'), pk=card_id)
        expire_stale_jobs(entry.deck)
        if entry.deck.jobs.filter(status__in=['pending', 'running']).exists():
            return Response({'detail': 'Hay una generación en curso para este mazo.'}, status=409)
        assignment = entry.deck.theme_assignments.filter(oracle_id=entry.card.oracle_id).first() if entry.card_id else None
        if assignment is None or not assignment.reference_wiki or not assignment.reference_page:
            return Response({'detail': 'Define una asignación con reference_wiki y reference_page.'}, status=400)
        force = request.data.get('force', True)
        if type(force) is not bool:
            return Response({'detail': 'force debe ser booleano.'}, status=400)
        fetch_auto_reference(assignment, force=force)
        return Response(DeckCardSerializer(entry, context=deck_detail_context(entry.deck, request)).data)
