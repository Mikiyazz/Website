"""页面路由：学生端 /news/、独立审核页 /reviews/news/、页面表单 POST 入口。"""
from django.urls import path

from . import views

urlpatterns = [
    path("news/", views.news_list_view, name="news-list-page"),
    path("news/edit/", views.news_edit_view, name="news-edit-page"),
    path("news/submissions/<uuid:news_id>/save/", views.news_save_page_view, name="news-save-page"),
    path("news/submissions/<uuid:news_id>/submit/", views.news_submit_page_view, name="news-submit-page"),
    path("reviews/news/", views.news_review_view, name="news-review-page"),
    path("reviews/news/<uuid:news_id>/action/", views.news_review_action_view, name="news-review-action"),
]
