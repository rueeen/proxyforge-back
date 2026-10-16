from django.urls import path

from .views import ThemeQuestionsView, ThemeAnswersView, ThemePlanView, ThemeAssignmentView, DeckEstimateView, GenerateDeckView, JobDetailView, RegenerateCardView

urlpatterns = [
    path("decks/<int:deck_id>/theme-questions/", ThemeQuestionsView.as_view(), name="theme-questions"),
    path("decks/<int:deck_id>/theme-answers/", ThemeAnswersView.as_view(), name="theme-answers"),
    path("decks/<int:deck_id>/theme-plan/", ThemePlanView.as_view(), name="theme-plan"),
    path("cards/<int:card_id>/theme-assignment/", ThemeAssignmentView.as_view(), name="theme-assignment"),
    path("decks/<int:deck_id>/generate/", GenerateDeckView.as_view(), name="generate-deck"),
    path("decks/<int:deck_id>/estimate/", DeckEstimateView.as_view(), name="deck-estimate"),
    path("jobs/<int:job_id>/", JobDetailView.as_view(), name="job-detail"),
    path("cards/<int:card_id>/regenerate/", RegenerateCardView.as_view(), name="regenerate-card"),
]
