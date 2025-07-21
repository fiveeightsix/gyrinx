import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.urls import reverse

from gyrinx.content.models import (
    ContentEquipment,
    ContentEquipmentCategory,
    ContentFighter,
    ContentFighterEquipmentListItem,
    ContentHouse,
)
from gyrinx.core.models import List, ListFighter
from gyrinx.models import FighterCategoryChoices

User = get_user_model()


def make_content_house(name):
    """Helper to create a content house."""
    return ContentHouse.objects.create(name=name)


def make_content_fighter(
    type, category, house, base_cost, can_take_legacy=False, can_be_legacy=False
):
    """Helper to create a content fighter."""
    return ContentFighter.objects.create(
        type=type,
        category=category,
        house=house,
        base_cost=base_cost,
        can_take_legacy=can_take_legacy,
        can_be_legacy=can_be_legacy,
    )


def make_equipment(name, category, cost):
    """Helper to create equipment."""
    return ContentEquipment.objects.create(
        name=name,
        category=category,
        cost=cost,
    )


@pytest.mark.django_db
def test_combined_equipment_lists_legacy_and_base():
    """
    Test that when a fighter has a legacy, equipment from both the legacy fighter
    and base fighter equipment lists are available.

    This tests the fix for issue #486 where Venator hunt leaders with gang legacy
    were losing access to psyker upgrade options.
    """
    # Create houses
    venator_house = make_content_house("House Venator")
    legacy_house = make_content_house("Legacy House")

    # Create fighters
    hunt_leader = make_content_fighter(
        type="Hunt Leader",
        category=FighterCategoryChoices.LEADER,
        house=venator_house,
        base_cost=100,
        can_take_legacy=True,
    )

    legacy_fighter = make_content_fighter(
        type="Legacy Champion",
        category=FighterCategoryChoices.CHAMPION,
        house=legacy_house,
        base_cost=100,
        can_be_legacy=True,
    )

    # Create equipment category
    options_category = ContentEquipmentCategory.objects.get_or_create(
        name="Options", defaults={"visible_only_if_in_equipment_list": False}
    )[0]

    # Create equipment - psyker options on base fighter
    non_sanctioned_psyker = make_equipment(
        "Non-sanctioned Psyker",
        category=options_category,
        cost=30,
    )
    sanctioned_psyker = make_equipment(
        "Sanctioned Psyker",
        category=options_category,
        cost=35,
    )

    # Create equipment - legacy equipment
    legacy_gear = make_equipment(
        "Legacy Gear",
        category=options_category,
        cost=50,
    )

    # Add psyker options to hunt leader's equipment list
    ContentFighterEquipmentListItem.objects.create(
        fighter=hunt_leader,
        equipment=non_sanctioned_psyker,
        cost=30,
    )
    ContentFighterEquipmentListItem.objects.create(
        fighter=hunt_leader,
        equipment=sanctioned_psyker,
        cost=35,
    )

    # Add legacy gear to legacy fighter's equipment list
    ContentFighterEquipmentListItem.objects.create(
        fighter=legacy_fighter,
        equipment=legacy_gear,
        cost=45,  # Discounted price
    )

    # Create list and fighter with legacy
    lst = List.objects.create(name="Test Venator Gang", content_house=venator_house)
    fighter = ListFighter.objects.create(
        name="Hunt Leader with Legacy",
        list=lst,
        content_fighter=hunt_leader,
        legacy_content_fighter=legacy_fighter,
    )

    # Test that equipment_list_fighters returns both fighters
    assert len(fighter.equipment_list_fighters) == 2
    assert legacy_fighter in fighter.equipment_list_fighters
    assert hunt_leader in fighter.equipment_list_fighters

    # Test that equipment from both lists is available
    # First, verify the equipment is in the respective lists
    hunt_leader_equipment = ContentFighterEquipmentListItem.objects.filter(
        fighter=hunt_leader
    ).values_list("equipment_id", flat=True)
    assert non_sanctioned_psyker.id in hunt_leader_equipment
    assert sanctioned_psyker.id in hunt_leader_equipment

    legacy_equipment = ContentFighterEquipmentListItem.objects.filter(
        fighter=legacy_fighter
    ).values_list("equipment_id", flat=True)
    assert legacy_gear.id in legacy_equipment

    # Now test combined query
    combined_equipment = ContentFighterEquipmentListItem.objects.filter(
        fighter__in=fighter.equipment_list_fighters
    ).values_list("equipment_id", flat=True)

    # Should include equipment from both fighters
    assert non_sanctioned_psyker.id in combined_equipment
    assert sanctioned_psyker.id in combined_equipment
    assert legacy_gear.id in combined_equipment

    # Test cost precedence - if same equipment exists on both lists, legacy takes precedence
    # Add the same psyker option to legacy fighter with different cost
    ContentFighterEquipmentListItem.objects.create(
        fighter=legacy_fighter,
        equipment=non_sanctioned_psyker,
        cost=25,  # Cheaper on legacy
    )

    # When getting cost, should use legacy price
    overrides = ContentFighterEquipmentListItem.objects.filter(
        fighter__in=fighter.equipment_list_fighters,
        equipment=non_sanctioned_psyker,
        weapon_profile=None,
    )

    # Should find 2 overrides (one from each fighter)
    assert overrides.count() == 2

    # Legacy override should be preferred
    legacy_override = overrides.filter(fighter=legacy_fighter).first()
    assert legacy_override.cost == 25


@pytest.mark.django_db
def test_trading_post_shows_combined_equipment():
    """
    Test that the trading post view shows equipment from both legacy and base fighters.
    """
    # Setup similar to above
    venator_house = make_content_house("House Venator")
    legacy_house = make_content_house("Legacy House")

    hunt_leader = make_content_fighter(
        type="Hunt Leader",
        category=FighterCategoryChoices.LEADER,
        house=venator_house,
        base_cost=100,
        can_take_legacy=True,
    )

    legacy_fighter = make_content_fighter(
        type="Legacy Champion",
        category=FighterCategoryChoices.CHAMPION,
        house=legacy_house,
        base_cost=100,
        can_be_legacy=True,
    )

    options_category = ContentEquipmentCategory.objects.get_or_create(
        name="Options", defaults={"visible_only_if_in_equipment_list": False}
    )[0]

    # Equipment only on base fighter
    base_only_equipment = make_equipment(
        "Base Fighter Equipment",
        category=options_category,
        cost=20,
    )
    ContentFighterEquipmentListItem.objects.create(
        fighter=hunt_leader,
        equipment=base_only_equipment,
        cost=20,
    )

    # Equipment only on legacy fighter
    legacy_only_equipment = make_equipment(
        "Legacy Fighter Equipment",
        category=options_category,
        cost=30,
    )
    ContentFighterEquipmentListItem.objects.create(
        fighter=legacy_fighter,
        equipment=legacy_only_equipment,
        cost=30,
    )

    # Create user, list, and fighter
    user = User.objects.create_user(username="testuser", password="testpass")
    lst = List.objects.create(
        name="Test Venator Gang",
        content_house=venator_house,
        owner=user,
    )
    fighter = ListFighter.objects.create(
        name="Hunt Leader with Legacy",
        list=lst,
        content_fighter=hunt_leader,
        legacy_content_fighter=legacy_fighter,
        owner=user,
    )

    # Test through the view
    client = Client()
    client.force_login(user)

    # Access trading post with equipment list filter
    url = reverse("core:list-fighter-gear-edit", args=[lst.id, fighter.id])
    response = client.get(url, {"filter": "equipment-list"})

    assert response.status_code == 200

    # Both equipment should be in the response context
    # The view stores VirtualListFighterEquipmentAssignment objects in 'assigns'
    equipment_ids = [assign.equipment.id for assign in response.context["assigns"]]
    assert base_only_equipment.id in equipment_ids
    assert legacy_only_equipment.id in equipment_ids
