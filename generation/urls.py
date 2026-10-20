from django.urls import path

from .views import ExportPdfView, ImageThumbnailView, OriginalArtView, BrowserReferencesView, WikiCandidatesView, ReferenceWikiView, WikiImagesView, WikiReferenceView, RewriteTextsView, CardFramingView, CardAutoFramingView, DeckAutoFramingView, AutoReferencesView, AutoReferenceView, AssignmentReferencesView, ThemeQuestionsView, ThemeAnswersView, ThemePlanView, ThemeAssignmentView, DeckEstimateView, GenerateDeckView, JobDetailView, RegenerateCardView

urlpatterns = [
    path("decks/<int:deck_id>/export-pdf/", ExportPdfView.as_view(), name="export-pdf"),
    path("images/<int:generated_id>/thumb/", ImageThumbnailView.as_view(), name="image-thumb"),
    path("cards/<int:card_id>/original-art/", OriginalArtView.as_view(), name="original-art"),
    path("cards/<int:card_id>/theme-assignment/references/browser/", BrowserReferencesView.as_view(), name="browser-references"),
    path("decks/<int:deck_id>/wiki-candidates/", WikiCandidatesView.as_view(), name="wiki-candidates"),
    path("decks/<int:deck_id>/reference-wiki/", ReferenceWikiView.as_view(), name="reference-wiki"),
    path("cards/<int:card_id>/theme-assignment/wiki-images/", WikiImagesView.as_view(), name="wiki-images"),
    path("cards/<int:card_id>/theme-assignment/references/wiki/", WikiReferenceView.as_view(), name="wiki-reference"),
    path("decks/<int:deck_id>/rewrite-texts/", RewriteTextsView.as_view(), name="rewrite-texts"),
    path("cards/<int:card_id>/framing/", CardFramingView.as_view(), name="card-framing"),
    path("cards/<int:card_id>/framing/auto/", CardAutoFramingView.as_view(), name="card-auto-framing"),
    path("decks/<int:deck_id>/framing/auto/", DeckAutoFramingView.as_view(), name="deck-auto-framing"),
    path("decks/<int:deck_id>/auto-references/", AutoReferencesView.as_view(), name="auto-references"),
    path("cards/<int:card_id>/theme-assignment/auto-reference/", AutoReferenceView.as_view(), name="auto-reference"),
    path("cards/<int:card_id>/theme-assignment/references/", AssignmentReferencesView.as_view(), name="assignment-references"),
    path("cards/<int:card_id>/theme-assignment/references/<int:reference_id>/", AssignmentReferencesView.as_view(), name="assignment-reference-detail"),
    path("decks/<int:deck_id>/theme-questions/", ThemeQuestionsView.as_view(), name="theme-questions"),
    path("decks/<int:deck_id>/theme-answers/", ThemeAnswersView.as_view(), name="theme-answers"),
    path("decks/<int:deck_id>/theme-plan/", ThemePlanView.as_view(), name="theme-plan"),
    path("cards/<int:card_id>/theme-assignment/", ThemeAssignmentView.as_view(), name="theme-assignment"),
    path("decks/<int:deck_id>/generate/", GenerateDeckView.as_view(), name="generate-deck"),
    path("decks/<int:deck_id>/estimate/", DeckEstimateView.as_view(), name="deck-estimate"),
    path("jobs/<int:job_id>/", JobDetailView.as_view(), name="job-detail"),
    path("cards/<int:card_id>/regenerate/", RegenerateCardView.as_view(), name="regenerate-card"),
]
