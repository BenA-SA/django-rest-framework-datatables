from types import SimpleNamespace

from albums.models import Album, Artist

from django.contrib.contenttypes.fields import GenericRel
from django.db.models import Count, F, Q, Sum, Window
from django.db.models.fields.reverse_related import OneToOneRel
from django.db.models.functions import RowNumber
from django.test import TestCase
from django.test.utils import override_settings
from django.urls import path

from rest_framework import serializers
from rest_framework.generics import ListAPIView

from rest_framework_datatables.filters import (
    DatatablesFilterBackend, is_to_many, order_by_one_value,
    repeats_objects)
from rest_framework_datatables.pagination import (
    DatatablesLimitOffsetPagination, DatatablesPageNumberPagination)

try:
    from django_filters import rest_framework as filters
    from rest_framework_datatables.django_filters.backends import (
        DatatablesFilterBackend as DjangoFilterBackend)
    from rest_framework_datatables.django_filters.filterset import (
        DatatablesFilterSet)
except ImportError:  # pragma: no cover
    DjangoFilterBackend = None


class RowSerializer(serializers.BaseSerializer):
    def to_representation(self, instance):
        if isinstance(instance, dict):
            return {key: str(value) for key, value in instance.items()}
        return {'name': instance.name}


QUERYSETS = {
    'all': lambda: Album.objects.all(),
    'duplicated_by_filter':
        lambda: Album.objects.filter(genres__name__icontains='rock'),
    'joined_by_extra': lambda: Album.objects.extra(
        tables=['albums_album_genres'],
        where=['albums_album_genres.album_id = albums_album.id']),
    'windowed': lambda: Album.objects.annotate(nth=Window(
        RowNumber(), partition_by=[F('artist')], order_by=F('year').asc())),
}
PAGINATIONS = {
    'page': DatatablesPageNumberPagination,
    'offset': DatatablesLimitOffsetPagination,
}


def view(queryset, pagination, backend=DatatablesFilterBackend, **attrs):
    return type('View', (ListAPIView,), {
        'serializer_class': RowSerializer,
        'filter_backends': [backend],
        'pagination_class': PAGINATIONS[pagination],
        'get_queryset': lambda self: QUERYSETS[queryset](),
        **attrs,
    }).as_view()


urlpatterns = [
    path(f'api/{pagination}/{queryset}/', view(queryset, pagination))
    for queryset in QUERYSETS for pagination in PAGINATIONS
] + [
    path(f'api/{pagination}/additional/', view(
        'all', pagination, datatables_additional_order_by='genres__name'))
    for pagination in PAGINATIONS
]

if DjangoFilterBackend is not None:
    class AlbumGenreFilter(DatatablesFilterSet):
        genres = filters.CharFilter(lookup_expr='name__icontains')

        class Meta:
            model = Album
            fields = ['name', 'genres']

    urlpatterns += [
        path(f'api/{pagination}/djangofilter/', view(
            'all', pagination, DjangoFilterBackend,
            filterset_class=AlbumGenreFilter))
        for pagination in PAGINATIONS
    ]

COLUMNS = (
    '&columns[0][data]=name&columns[0][searchable]=true'
    '&columns[0][orderable]=true'
    '&columns[1][data]=genres&columns[1][name]=genres__name'
    '&columns[1][searchable]=true&columns[1][orderable]=true'
    '&columns[2][data]=genres&columns[2][orderable]=true'
)
BY_GENRE = '&order[0][column]=1&order[0][dir]=%s'
BY_NAME = '&order[0][column]=0&order[0][dir]=asc'
BY_RELATION = '&order[0][column]=2&order[0][dir]=asc'


class DistinctCountsTestCase(TestCase):
    fixtures = ['test_data']
    paginations = PAGINATIONS

    def rows(self, url):
        """all the rows a table can page through, and its count"""
        rows = self.client.get(url + '&length=-1').json()['data']
        page = self.client.get(url + '&length=10').json()
        return [row.get('name') for row in rows], page['recordsFiltered']

    def assert_counts_the_rows(self, endpoint, query=''):
        for pagination in self.paginations:
            with self.subTest(pagination=pagination):
                url = (f'/api/{pagination}/{endpoint}/?format=datatables'
                       + COLUMNS + query)
                rows, count = self.rows(url)
                self.assertEqual(count, len(rows))

    def assert_sorting_keeps_the_rows(self, endpoint, query, search=''):
        self.assert_counts_the_rows(endpoint, search + query)
        for pagination in PAGINATIONS:
            with self.subTest(pagination=pagination):
                url = (f'/api/{pagination}/{endpoint}/?format=datatables'
                       + COLUMNS + search)
                unsorted, count = self.rows(url)
                rows, count = self.rows(url + query)
                self.assertEqual(sorted(rows), sorted(unsorted))

    def assert_each_row_once(self, endpoint, query=''):
        self.assert_counts_the_rows(endpoint, query)
        for pagination in PAGINATIONS:
            with self.subTest(pagination=pagination):
                url = (f'/api/{pagination}/{endpoint}/?format=datatables'
                       + COLUMNS + query)
                rows, count = self.rows(url)
                self.assertEqual(len(rows), len(set(rows)))


@override_settings(ROOT_URLCONF=__name__)
class TestOrderingByToManyColumn(DistinctCountsTestCase):
    """Ordering by a to-many column shows each row once

    Ordering by a related field joins every related row, so each album
    came back once per genre, while the count reported albums.

    """

    def test_ordered(self):
        for direction in ('asc', 'desc'):
            with self.subTest(direction=direction):
                self.assert_each_row_once('all', BY_GENRE % direction)

    def test_searched_and_ordered(self):
        for direction in ('asc', 'desc'):
            with self.subTest(direction=direction):
                self.assert_each_row_once(
                    'all', '&search[value]=o' + BY_GENRE % direction)

    def test_additional_ordering(self):
        self.assert_each_row_once('additional', BY_NAME)

    def test_django_filter_backend(self):
        if DjangoFilterBackend is None:  # pragma: no cover
            self.skipTest('django-filter not available')
        for direction in ('asc', 'desc'):
            with self.subTest(direction=direction):
                self.assert_each_row_once(
                    'djangofilter', BY_GENRE % direction)

    def test_keeps_rows_the_view_duplicates(self):
        """Sorting changes the order of the rows, never which rows

        A view whose queryset already repeats a row, through a filter or
        an alias across a to-many relation, shows the same rows sorted.

        """
        for endpoint in ('duplicated_by_filter', 'joined_by_extra'):
            for direction in ('asc', 'desc'):
                with self.subTest(endpoint=endpoint, direction=direction):
                    self.assert_sorting_keeps_the_rows(
                        endpoint, BY_GENRE % direction)

    def test_keeps_rows_a_window_numbers(self):
        """A window numbers each row before the search makes them distinct"""
        self.assert_sorting_keeps_the_rows(
            'windowed', BY_GENRE % 'asc', '&search[value]=o')

    def test_orders_a_relation_by_its_model_ordering(self):
        """A column of the relation itself sorts as Django sorts it

        Django orders by a relation using the related model's ordering,
        so genres by name, not by primary key.

        """
        url = '/api/page/all/?format=datatables' + COLUMNS + BY_RELATION
        rows, count = self.rows(url)
        self.assertEqual(rows[0], 'The Velvet Underground & Nico')

    def test_orders_by_the_value_that_matched(self):
        """A search on the column orders by the related value it matched

        Exile on Main St. has Rock & Roll among its genres, but only
        Blues Rock matches, so Blonde on Blonde, matched by Rhythm &
        Blues, comes first in descending order.

        """
        url = ('/api/page/all/?format=datatables' + COLUMNS
               + '&search[value]=blues' + BY_GENRE % 'desc')
        rows, count = self.rows(url)
        self.assertEqual(rows[0], 'Blonde on Blonde')


class TestIsToMany(TestCase):
    def test_lookups(self):
        for model, lookup, expected in (
                (Album, 'genres', True),
                (Album, 'genres__name', True),
                (Artist, 'albums__name', True),
                (Album, 'name', False),
                (Album, 'artist', False),
                (Album, 'artist__name', False),
                (Album, '?', False),
                (Album, 'not_a_field', False)):
            with self.subTest(lookup=lookup):
                self.assertIs(is_to_many(model, lookup), expected)


class TestOrderByOneValue(TestCase):
    """Each ordering term across a to-many relation orders by one value"""

    fixtures = ['test_data']

    def test_terms(self):
        for term in (
                'genres__name', '-genres__name',
                'genres', '-genres', 'genres__pk'):
            with self.subTest(term=term):
                names = [album.name for album in
                         order_by_one_value(Album.objects.all(), [term])]
                self.assertEqual(len(names), Album.objects.count())
                self.assertEqual(len(names), len(set(names)))

    def test_orders_unique_rows_by_an_aggregate(self):
        """A subquery is only used where the view's joins repeat rows

        A correlated subquery runs once per row, so rows that are already
        one per object, with no joins or with DISTINCT, are ordered by an
        aggregate instead.

        """
        searched = Q(name__icontains='o') | Q(genres__name__icontains='o')
        for name, queryset, subqueries in (
                ('no joins', Album.objects.all(), 0),
                ('distinct', Album.objects.filter(searched).distinct(), 0),
                ('to-one join',
                 Album.objects.filter(artist__name__icontains='a'), 0),
                ('own aggregate',
                 Album.objects.annotate(total=Sum('rank')), 1),
                ('extra column',
                 Album.objects.extra(select={'double': 'rank * 2'}), 1),
                ('repeated rows',
                 Album.objects.filter(genres__name__icontains='rock'), 1)):
            with self.subTest(name):
                ordered = order_by_one_value(queryset, ['genres__name'])
                sql = str(ordered.query)
                self.assertEqual(sql.count('SELECT') - 1, subqueries)

    def test_leaves_projections_as_they_are(self):
        """values() and grouped querysets keep the ordering as given"""
        for queryset in (
                Album.objects.values('artist'),
                Album.objects.values('artist').annotate(albums=Count('pk'))):
            with self.subTest(annotations=list(queryset.query.annotations)):
                ordered = order_by_one_value(queryset, ['genres__name'])
                self.assertEqual(ordered.query.order_by, ('genres__name',))
                self.assertEqual(
                    list(ordered.query.annotations),
                    list(queryset.query.annotations))

    def test_keeps_the_view_annotations(self):
        """The value sorted by never replaces an annotation of the view"""
        queryset = Album.objects.annotate(_datatables_order_0=F('year'))
        ordered = order_by_one_value(queryset, ['genres__name'])
        self.assertEqual(
            sorted(album._datatables_order_0 for album in ordered),
            sorted(Album.objects.values_list('year', flat=True)))

    def test_other_terms_are_kept(self):
        for term in ('name', '-year', F('year').desc(), '?'):
            with self.subTest(term=term):
                queryset = order_by_one_value(Album.objects.all(), [term])
                self.assertEqual(queryset.query.order_by, (term,))


class TestRepeatsObjects(TestCase):
    """A join repeats an object when it reaches many related rows"""

    def test_queries(self):
        for name, queryset, expected in (
                ('no join', Album.objects.all(), False),
                ('to-one join', Album.objects.filter(artist__name='x'), False),
                ('many to many', Album.objects.filter(genres__name='x'), True),
                ('reverse foreign key',
                 Artist.objects.filter(albums__name='x'), True),
                ('extra table', Album.objects.extra(
                    tables=['albums_album_genres']), True)):
            with self.subTest(name):
                self.assertIs(repeats_objects(queryset.query), expected)

    def test_reverse_relations(self):
        for name, field, expected in (
                ('generic relation', GenericRel(None, Album), True),
                ('reverse one to one', OneToOneRel(None, Album, 'id'), False)):
            with self.subTest(name):
                query = SimpleNamespace(extra_tables=(), alias_map={
                    'joined': SimpleNamespace(join_field=field)})
                self.assertIs(repeats_objects(query), expected)
