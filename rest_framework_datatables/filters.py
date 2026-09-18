import operator
import re
import sys
from functools import reduce

import django
from django.core.exceptions import FieldDoesNotExist
from django.db.models import (
    F, ForeignObjectRel, Max, Min, OuterRef, Q, Subquery)
from django.db.models.constants import LOOKUP_SEP
from django.db.models.expressions import OrderBy
from django.db.models.functions import Random
from rest_framework.filters import BaseFilterBackend

from .utils import get_param


def is_valid_regex(regex):
    """helper function that checks regex for validity"""
    try:
        re.compile(regex)
        return True
    except re.error:
        return False


def f_search_q(f, search_value, search_regex=False):
    """helper function that returns a Q-object for a search value"""
    qs = []
    if search_value and search_value != 'false':
        if search_regex:
            if is_valid_regex(search_value):
                for x in f['name']:
                    qs.append(Q(**{'%s__iregex' % x: search_value}))
        else:
            for x in f['name']:
                qs.append(Q(**{'%s__icontains' % x: search_value}))
    return reduce(operator.or_, qs, Q())


def is_to_many(model, lookup):
    """helper function that tells if a lookup crosses a to-many relation"""
    for part in lookup.split(LOOKUP_SEP):
        try:
            field = model._meta.get_field(part)
        except FieldDoesNotExist:
            return False
        if field.many_to_many or field.one_to_many:
            return True
        if not field.is_relation:
            return False
        model = field.related_model
    return False


def related_ordering(model, lookup):
    """helper function that spells out how Django orders a relation

    Ordering by a relation itself orders by the related model's first
    ordering field, or its primary key. Returns the lookup to order by,
    and whether that field orders descending.

    """
    for part in lookup.split(LOOKUP_SEP):
        try:
            field = model._meta.get_field(part)
        except FieldDoesNotExist:
            return lookup, False
        if not field.is_relation:
            return lookup, False
        model = field.related_model
    ordering = [term for term in model._meta.ordering if isinstance(term, str)]
    first = ordering[0] if ordering else 'pk'
    return lookup + LOOKUP_SEP + first.lstrip('-'), first.startswith('-')


def repeats_objects(query):
    """helper function that tells if a query's joins can repeat an object

    A join across a to-many relation, or a table added with extra(), can
    return an object once for each related row; a join to one related
    object cannot.

    """
    if query.extra_tables:
        return True
    for join in query.alias_map.values():
        field = getattr(join, 'join_field', None)
        if field is not None and reaches_many(field):
            return True
    return False


def reaches_many(field):
    """helper function that tells if a join field reaches many rows

    A reverse relation, including a generic one, says so itself; a
    forward field by its kind.

    """
    if isinstance(field, ForeignObjectRel):
        return field.multiple
    return field.one_to_many or field.many_to_many


def aggregates_safely(query):
    """helper function that tells if a query can be ordered by an aggregate

    An aggregate groups the rows by object, which leaves them as they
    are only when each object is already one row, with DISTINCT or with
    no join that repeats it. Its own aggregates and windows would change
    with the grouping or the added join, and DISTINCT compares its
    extra() columns, so a query with any of those is not grouped.

    """
    if query.group_by is not None or query.extra:
        return False
    if any(getattr(annotation, 'contains_over_clause', False)
           for annotation in query.annotations.values()):
        return False
    return query.distinct or not repeats_objects(query)


def one_value_ordering(queryset, term, name):
    """helper function that orders by one related value instead of each

    Returns the annotations to add and the ordering term to use in place
    of term: the lowest related value, or the highest when term is
    descending, keeping where term places nulls. The value comes from the
    rows the queryset keeps, so a search on the relation orders by the
    values it matched. It is an aggregate, added with alias() so a count
    leaves it out, unless grouping would merge rows the queryset
    returns; a subquery then takes the value for each row, once per row.

    """
    nulls = {}
    if isinstance(term, str):
        lookup, flipped = related_ordering(
            queryset.model, term.lstrip('-'))
        descending = term.startswith('-') != flipped
        expression = F(lookup)
    elif isinstance(term, OrderBy):
        descending, expression = term.descending, term.expression
        nulls = {'nulls_first': term.nulls_first,
                 'nulls_last': term.nulls_last}
    else:
        descending, expression = False, term
    aggregate = (Max if descending else Min)(expression)
    if aggregates_safely(queryset.query):
        ordered = OrderBy(F(name), descending=descending, **nulls)
        return {name: aggregate}, ordered
    values = queryset.order_by().filter(pk=OuterRef('pk')).values('pk')
    values = values.annotate(_datatables_value=aggregate)
    subquery = Subquery(values.values('_datatables_value'))
    return {}, OrderBy(subquery, descending=descending, **nulls)


def unused_name(query, position):
    """helper function that names a sort value no annotation already uses"""
    name = '_datatables_order_%d' % position
    while name in query.annotations:
        name += '_'
    return name


def order_by_one_value(queryset, ordering):
    """helper function that orders a queryset without repeating its rows

    Ordering by a field across a to-many relation joins every related
    row, so each row came back once per related object. Such a term is
    ordered by one related value instead, whether it is a field name or
    an expression, leaving the rows as the queryset returns them. A
    values() queryset, whose rows are not objects, is ordered as given.

    """
    if queryset.query.values_select:
        return queryset.order_by(*ordering)
    annotations = {}
    order_by = []
    for term in ordering:
        if not any(is_to_many(queryset.model, lookup)
                   for lookup in ordering_lookups(term)):
            order_by.append(term)
            continue
        name = unused_name(queryset.query, len(order_by))
        added, ordered = one_value_ordering(queryset, term, name)
        annotations.update(added)
        order_by.append(ordered)
    if annotations:
        queryset = queryset.alias(**annotations)
    return queryset.order_by(*order_by)


def ordering_lookups(term):
    """helper function that lists the field lookups an ordering term uses

    An expression is searched for F() references and for the lookups of
    its Q() conditions, such as those of a When() inside a Case().

    """
    if isinstance(term, str):
        return [term.lstrip('-')]
    if isinstance(term, F):
        return [term.name]
    lookups = []
    if isinstance(term, Q):
        for child in term.children:
            if isinstance(child, tuple):
                lookups.append(child[0])
            else:
                lookups.extend(ordering_lookups(child))
        return lookups
    for source in term.get_source_expressions():
        if source is not None:
            lookups.extend(ordering_lookups(source))
    return lookups


def is_random(term):
    """helper function that tells if an ordering term orders randomly"""
    if isinstance(term, OrderBy):
        term = term.expression
    return term == '?' or isinstance(term, Random)


def adds_compared_column(query, term):
    """helper function that tells if an ordering column changes the rows

    DISTINCT compares the ordering columns too, and GROUP BY groups by
    them. The columns of an object include its primary key, so only a
    random order makes its rows distinct; a values() projection also
    gains any column it does not select.

    """
    if is_random(term):
        return True
    if not query.values_select:
        return False
    selected = set(query.values_select) | set(query.annotation_select)
    return not set(ordering_lookups(term)) <= selected


def query_ordering(query):
    """helper function that gives the ordering a query returns rows in

    From Django 4.0 on, the model's default ordering is left out of a
    GROUP BY query; before, it still groups by the ordering's columns.

    """
    if query.order_by:
        return query.order_by
    if not query.default_ordering:
        return ()
    if query.group_by is not None and django.VERSION >= (4, 0):
        return ()
    return query.get_meta().ordering


def ordering_changes_rows(queryset):
    """helper function that tells if a queryset's ordering adds rows

    Ordering across a to-many relation joins every related row, and
    with DISTINCT or GROUP BY, Django also selects the ordering columns,
    which can split rows that would otherwise be one. DISTINCT ON
    decides the rows itself.

    """
    query = queryset.query
    if query.distinct_fields:
        return False
    compared = query.distinct or query.group_by is not None
    for term in query_ordering(query):
        if any(is_to_many(queryset.model, lookup)
               for lookup in ordering_lookups(term)):
            return True
        if compared and adds_compared_column(query, term):
            return True
    return False


def count_rows(queryset):
    """helper function that counts the rows a queryset returns

    count() drops the ordering, which is right unless the ordering adds
    rows; then the queryset is counted with its ordering, which Django
    keeps when the queryset is sliced.

    """
    if not ordering_changes_rows(queryset):
        return queryset.count()
    if queryset.query.values_select:
        queryset = with_named_ordering(queryset)
    return queryset[:sys.maxsize].count()


def with_named_ordering(queryset):
    """helper function that orders a values() queryset by named columns

    A selected column can share its name with a column the queryset is
    ordered by, such as a name across a relation, which Postgres cannot
    tell apart once the ordered queryset is counted. Each ordering field
    is selected under a name of its own and ordered by that instead.

    """
    aliases = {}
    order_by = []
    for term in query_ordering(queryset.query):
        if not isinstance(term, str) or is_random(term):
            order_by.append(term)
            continue
        name = unused_name(queryset.query, len(aliases))
        aliases[name] = F(term.lstrip('-'))
        order_by.append(('-' if term.startswith('-') else '') + name)
    return queryset.annotate(**aliases).order_by(*order_by)


def counted_as_sorted(queryset, ordering):
    """helper function that gives the queryset to count for a sorted table

    The sort replaces the queryset's own ordering, and adds no rows
    unless DISTINCT compares what it sorts by, so the queryset is counted
    without an ordering, which keeps the sort out of the count.

    """
    if not ordering:
        return queryset
    ordered = order_by_one_value(queryset, ordering)
    return ordered if ordering_changes_rows(ordered) else queryset.order_by()


class DatatablesBaseFilterBackend(BaseFilterBackend):
    """Base class for definining your own DatatablesFilterBackend classes"""

    def check_renderer_format(self, request):
        return request.accepted_renderer.format == 'datatables'

    def parse_datatables_query(self, request, view):
        """parse request.query_params into a list of fields and orderings and
        global search parameters (value and regex)"""
        ret = {}
        ret['fields'] = self.get_fields(request)
        ret['search_value'] = get_param(request, 'search[value]')
        ret['search_regex'] = get_param(request, 'search[regex]') == 'true'
        return ret

    def get_fields(self, request):
        """called by parse_query_params to get the list of fields"""
        fields = []
        i = 0
        while True:
            col = 'columns[%d][%s]'
            data = get_param(request, col % (i, 'data'))
            if data == "":  # null or empty string on datatables (JS) side
                fields.append({'searchable': False, 'orderable': False})
                i += 1
                continue
            # break out only when there are no more fields to get.
            if data is None:
                break
            name = get_param(request, col % (i, 'name'))
            if not name:
                name = data
            search_col = col % (i, 'search')
            # to be able to search across multiple fields (e.g. to search
            # through concatenated names), we create a list of the name field,
            # replacing dot notation with double-underscores and splitting
            # along the commas.
            field = {
                'name': [
                    n.lstrip() for n in name.replace('.', '__').split(',')
                ],
                'data': data,
                'searchable': get_param(
                    request, col % (i, 'searchable')
                ) == 'true',
                'orderable': get_param(
                    request, col % (i, 'orderable')
                ) == 'true',
                'search_value': get_param(
                    request, '%s[%s]' % (search_col, 'value')
                ),
                'search_regex': get_param(
                    request, '%s[%s]' % (search_col, 'regex')
                ) == 'true',
            }
            fields.append(field)
            i += 1
        return fields

    def get_ordering_fields(self, request, view, fields):
        """called by parse_query_params to get the ordering

        return value must be a list of tuples.
        (field, dir)

        field is the field to order by and dir is the direction of the
        ordering ('asc' or 'desc').

        """
        ret = []
        i = 0
        while True:
            col = 'order[%d][%s]'
            idx = get_param(request, col % (i, 'column'))
            if idx is None:
                break
            try:
                field = fields[int(idx)]
            except IndexError:
                i += 1
                continue
            if not field['orderable']:
                i += 1
                continue
            dir_ = get_param(request, col % (i, 'dir'), 'asc')
            ret.append((field, dir_))
            i += 1
        return ret

    def set_count_before(self, view, total_count):
        # set the queryset count as an attribute of the view for later
        # TODO: find a better way than this hack
        setattr(view, '_datatables_total_count', total_count)

    def set_count_after(self, view, filtered_count):
        """called by filter_queryset to store the ordering after the filter
        operations

        """
        # set the queryset count as an attribute of the view for later
        # TODO: maybe find a better way than this hack ?
        setattr(view, '_datatables_filtered_count', filtered_count)

    def append_additional_ordering(self, ordering, view):
        if len(ordering):
            if hasattr(view, 'datatables_additional_order_by'):
                additional = view.datatables_additional_order_by
                # Django will actually only take the first occurrence if the
                # same column is added multiple times in an order_by, but it
                # feels cleaner to double check for duplicate anyway.
                if not any((o[1:] if o[0] == '-' else o) == additional
                           for o in ordering):
                    ordering.append(additional)


class DatatablesFilterBackend(DatatablesBaseFilterBackend):
    """
    Filter that works with datatables params.
    """

    def filter_queryset(self, request, queryset, view):
        """filter the queryset

        subclasses overriding this method should make sure to do all
        necessary steps

        -  Return unfiltered queryset if accepted renderer format is
           not 'datatables' (via `check_renderer_format`)

        - store the counts before and after filtering with
          `set_count_before` and `set_count_after`

        - respect ordering (in `ordering` key of parsed datatables
          query)

        """
        if not self.check_renderer_format(request):
            return queryset

        datatables_query = self.parse_datatables_query(request, view)
        ordering = self.get_ordering(request, view, datatables_query['fields'])

        total_count = count_rows(
            counted_as_sorted(view.get_queryset(), ordering))
        self.set_count_before(view, total_count)
        # another filter backend on the view may have changed the rows
        unchanged = len(getattr(view, 'filter_backends', [])) <= 1

        q = self.get_q(datatables_query)
        if q:
            queryset = queryset.filter(q).distinct()
            unchanged = False
        if not unchanged:
            filtered_count = count_rows(counted_as_sorted(queryset, ordering))
        else:
            filtered_count = total_count
        self.set_count_after(view, filtered_count)

        if ordering:
            queryset = order_by_one_value(queryset, ordering)
        return queryset

    def get_q(self, datatables_query):
        q = Q()
        initial_q = Q()
        for f in datatables_query['fields']:
            if not f['searchable']:
                continue
            q |= f_search_q(f,
                            datatables_query['search_value'],
                            datatables_query['search_regex'])
            initial_q &= f_search_q(f,
                                    f.get('search_value'),
                                    f.get('search_regex', False))
        q &= initial_q
        return q

    def get_ordering(self, request, view, fields):
        """called by parse_query_params to get the ordering

        return value must be a valid list of arguments for order_by on
        a queryset

        """
        ordering = []
        for field, dir_ in self.get_ordering_fields(request, view, fields):
            ordering.append('%s%s' % (
                '-' if dir_ == 'desc' else '',
                field['name'][0]
            ))
        self.append_additional_ordering(ordering, view)
        return ordering
