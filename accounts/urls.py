from django.urls import include, path
from rest_framework.authtoken.views import obtain_auth_token

from . import views

urlpatterns = [
    path("login/", views.LoginView.as_view(), name="login"),
    path("token/", obtain_auth_token, name="token"),
    path("me/", views.MeView.as_view(), name="me"),
    path(
        "assignments/",
        views.ProjectAssignmentList.as_view(),
        name="project-assignment-list",
    ),
    path(
        "memberships/",
        views.CrewMembershipList.as_view(),
        name="crew-membership-list",
    ),
]
