import logging
from dataclasses import dataclass
from typing import Optional

from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Case, Count, F, Q, When
from multiselectfield import MultiSelectField
from polymorphic.models import PolymorphicModel
from simple_history.models import HistoricalRecords

from gyrinx.content.models import (
    Content,
    ContentAttribute,
    ContentAttributeValue,
    ContentEquipment,
    ContentHouse,
    ContentWeaponProfile,
)
from gyrinx.core.models.list import List, ListFighter
from gyrinx.models import (
    FighterCategoryChoices,
    format_cost_display,
)

logger = logging.getLogger(__name__)

##
## Equipment List Expansion Models
##


@dataclass
class ExpansionRuleInputs:
    """Inputs for evaluating equipment list expansion rules."""

    list: Optional[List] = None
    fighter: Optional[ListFighter] = None


class ContentEquipmentListExpansion(Content):
    """
    Represents an expansion to equipment lists based on certain conditions.
    When all rules match for a fighter/list, the expansion items become available.
    """

    help_text = "An expansion to equipment lists that applies based on gang attributes, house, or fighter categories."
    name = models.CharField(
        max_length=255, unique=True, help_text="Name of this equipment list expansion"
    )
    rules = models.ManyToManyField(
        "ContentEquipmentListExpansionRule",
        related_name="expansions",
        help_text="All rules must match (AND logic) for this expansion to apply",
    )

    history = HistoricalRecords()

    def applies_to(self, rule_inputs: ExpansionRuleInputs) -> bool:
        """
        Check if this expansion applies to the given list and fighter.
        All rules must match (AND logic).
        """

        # All rules must match
        for rule in self.rules.all():
            if not rule.match(rule_inputs):
                return False

        return True

    @classmethod
    def get_applicable_expansions(cls, rule_inputs: ExpansionRuleInputs):
        """
        Get all expansions that apply to the given rule inputs.
        """

        input_list = rule_inputs.list
        input_fighter = rule_inputs.fighter

        # First we find all the rules that match the list and fighter
        list_rules = Q(
            ContentEquipmentListExpansionRuleByAttribute___attribute_values__in=[
                aa.attribute_value.id for aa in input_list.active_attributes_cached
            ]
        ) | Q(ContentEquipmentListExpansionRuleByHouse___house=input_list.content_house)

        fighter_rules = (
            Q(
                ContentEquipmentListExpansionRuleByFighterCategory___fighter_categories__contains=input_fighter.get_category()
            )
            if input_fighter
            else None
        )

        # This gets us all the rules that match the inputs
        applicable_rules = ContentEquipmentListExpansionRule.objects.filter(
            (list_rules | fighter_rules) if fighter_rules else list_rules
        ).distinct()

        # Then we need to find expansions that have _all_ these rules matching, and only these
        applicable_expansions = (
            cls.objects
            # Count how many of this expansion's rules are in applicable_rules
            .annotate(
                matched=Count(
                    "rules", filter=Q(rules__in=applicable_rules), distinct=True
                ),
                total=Count("rules", distinct=True),
            )
            .filter(matched=F("total"))
            .exclude(total=0)
            .prefetch_related(
                "rules",
                "items__equipment",
                "items__weapon_profile",
            )
        )

        return applicable_expansions

    @classmethod
    def get_applicable_expansion_items_for_equipment(
        cls,
        rule_inputs: ExpansionRuleInputs,
        equipment: ContentEquipment,
        weapon_profile: Optional[ContentWeaponProfile] = None,
        **kwargs,
    ):
        """
        Return expansion items for the specified equipment (and optional weapon profile)
        that are available due to applicable expansions for the given rule inputs.
        """
        # First check if this equipment/profile combination exists in any expansion
        exists_in_expansion = ContentEquipmentListExpansionItem.objects.filter(
            equipment=equipment,
            weapon_profile=weapon_profile,
        ).exists()

        # If not in any expansion, return empty queryset to avoid expansion checks
        if not exists_in_expansion:
            return ContentEquipmentListExpansionItem.objects.none()

        # Filter expansions that include the specified equipment and optionally weapon profile
        return ContentEquipmentListExpansionItem.objects.filter(
            equipment=equipment,
            weapon_profile=weapon_profile,
            expansion__in=cls.get_applicable_expansions(rule_inputs),
            **kwargs,
        )

    @classmethod
    def get_expansion_equipment(cls, rule_inputs: ExpansionRuleInputs):
        """
        Get all equipment available from expansions based on rule inputs.
        Returns a queryset of ContentEquipment with cost annotations.
        Also includes weapon profiles when specified.
        """
        expansions = cls.get_applicable_expansions(rule_inputs)

        # Get all equipment IDs and profile IDs from applicable expansions
        equipment_data = {}  # Maps equipment_id -> {cost, profiles: {profile_id: cost}}

        # Prefetch items to avoid N+1 queries
        prefetched_expansions = cls.objects.filter(
            id__in=[e.id for e in expansions]
        ).prefetch_related("items", "items__weapon_profile")

        for expansion in prefetched_expansions:
            for item in expansion.items.all():
                eq_id = item.equipment_id

                if eq_id not in equipment_data:
                    equipment_data[eq_id] = {"cost": None, "profiles": {}}

                if item.weapon_profile_id:
                    # This is a specific weapon profile
                    equipment_data[eq_id]["profiles"][item.weapon_profile_id] = (
                        item.cost
                    )
                else:
                    # This is base equipment
                    equipment_data[eq_id]["cost"] = item.cost

        # Get the equipment and annotate with cost overrides
        equipment_ids = list(equipment_data.keys())
        equipment = ContentEquipment.objects.filter(id__in=equipment_ids)

        # Apply cost overrides using Case/When
        cost_overrides = {
            eq_id: data["cost"]
            for eq_id, data in equipment_data.items()
            if data["cost"] is not None
        }

        if cost_overrides:
            when_clauses = [
                When(id=eq_id, then=cost) for eq_id, cost in cost_overrides.items()
            ]
            equipment = equipment.annotate(
                expansion_cost_override=Case(
                    *when_clauses,
                    default=models.F("cost_cast_int"),
                    output_field=models.IntegerField(),
                )
            )
        else:
            equipment = equipment.annotate(
                expansion_cost_override=models.F("cost_cast_int")
            )

        return equipment

    def __str__(self):
        return self.name

    class Meta:
        verbose_name = "Equipment List Expansion"
        verbose_name_plural = "Equipment List Expansions"
        ordering = ["name"]


class ContentEquipmentListExpansionItem(Content):
    """
    Represents a single equipment item that becomes available as part of an expansion.
    """

    help_text = "A piece of equipment that becomes available as part of an expansion."
    expansion = models.ForeignKey(
        ContentEquipmentListExpansion,
        on_delete=models.CASCADE,
        related_name="items",
        help_text="The expansion this item belongs to",
    )
    equipment = models.ForeignKey(
        ContentEquipment,
        on_delete=models.CASCADE,
        help_text="The equipment that becomes available",
    )
    weapon_profile = models.ForeignKey(
        "ContentWeaponProfile",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        help_text="The weapon profile to use for this expansion item (e.g., specific ammo type)",
    )
    cost = models.IntegerField(
        null=True,
        blank=True,
        help_text="Override cost for this equipment in the expansion (null = use base cost)",
    )

    history = HistoricalRecords()

    def __str__(self):
        cost_str = (
            f" ({format_cost_display(self.cost)})" if self.cost is not None else ""
        )
        profile_str = f" - {self.weapon_profile.name}" if self.weapon_profile else ""
        return f"{self.equipment.name}{profile_str}{cost_str} in {self.expansion.name}"

    def clean(self):
        """
        Validation to ensure that the weapon profile matches the correct equipment.
        """
        if self.weapon_profile and self.weapon_profile.equipment != self.equipment:
            raise ValidationError(
                {"weapon_profile": "Weapon profile must match the equipment selected."}
            )

    class Meta:
        verbose_name = "Equipment List Expansion Item"
        verbose_name_plural = "Equipment List Expansion Items"
        unique_together = ["expansion", "equipment", "weapon_profile"]
        ordering = ["expansion", "equipment__name"]


class ContentEquipmentListExpansionRule(PolymorphicModel, Content):
    """
    Base polymorphic model for expansion rules.
    Each subclass implements specific matching logic.
    """

    help_text = "Base class for equipment list expansion rules."
    history = HistoricalRecords()

    def match(self, rule_inputs) -> bool:
        """
        Check if this rule matches the given inputs.

        Args:
            rule_inputs: A dataclass containing the list and fighter to evaluate.

        Returns:
            bool: True if the rule matches, False otherwise.
        """
        raise NotImplementedError("Subclasses must implement match()")

    def __str__(self):
        return "Base Expansion Rule"

    class Meta:
        verbose_name = "Equipment List Expansion Rule"
        verbose_name_plural = "Equipment List Expansion Rules"


class ContentEquipmentListExpansionRuleByAttribute(ContentEquipmentListExpansionRule):
    """
    Rule that matches based on gang attributes.
    If no attribute values are specified, matches any value except "not set".
    """

    help_text = (
        "Rule that matches based on gang attributes (e.g., affiliation, alignment)."
    )
    attribute = models.ForeignKey(
        ContentAttribute,
        on_delete=models.CASCADE,
        help_text="The attribute to match on",
    )
    attribute_values = models.ManyToManyField(
        ContentAttributeValue,
        blank=True,
        help_text="Specific values to match (empty = any value except 'not set')",
    )

    def match(self, rule_inputs: ExpansionRuleInputs) -> bool:
        """Check if the list has the required attribute value."""
        list_obj: "List" = rule_inputs.list

        # Get the list's attribute values for this attribute
        list_values = list_obj.attributes.filter(
            attribute=self.attribute, listattributeassignment__archived=False
        )

        # If no list values, the rule doesn't match
        if not list_values.exists():
            return False

        # Get the specified values for this rule
        rule_values = self.attribute_values.all()

        # If no specific values specified, match any value (except not having the attribute)
        if not rule_values.exists():
            return True

        # Check if any of the list's values match the rule's values
        return any(list_value in rule_values for list_value in list_values)

    def __str__(self):
        values = self.attribute_values.all()
        if values.exists():
            values_str = ", ".join(str(v) for v in values[:3])
            if values.count() > 3:
                values_str += "..."
        else:
            values_str = "any"
        return f"Attribute Rule: {self.attribute} = {values_str}"

    class Meta:
        verbose_name = "Expansion Rule by Attribute"
        verbose_name_plural = "Expansion Rules by Attribute"


class ContentEquipmentListExpansionRuleByHouse(ContentEquipmentListExpansionRule):
    """
    Rule that matches based on the gang's house.
    """

    help_text = "Rule that matches based on the gang's house (e.g., Delaque, Goliath)."
    house = models.ForeignKey(
        ContentHouse,
        on_delete=models.CASCADE,
        help_text="The house to match",
    )

    def match(self, rule_inputs) -> bool:
        """Check if the list belongs to the required house."""
        return rule_inputs.list.content_house == self.house

    def __str__(self):
        return f"House Rule: {self.house}"

    class Meta:
        verbose_name = "Expansion Rule by House"
        verbose_name_plural = "Expansion Rules by House"


class ContentEquipmentListExpansionRuleByFighterCategory(
    ContentEquipmentListExpansionRule
):
    """
    Rule that matches based on fighter categories.
    """

    help_text = "Rule that matches based on fighter categories (e.g., Leader, Champion, Vehicle)."
    fighter_categories = MultiSelectField(
        choices=FighterCategoryChoices.choices,
        help_text="Fighter categories that must match",
    )

    def match(self, rule_inputs: ExpansionRuleInputs) -> bool:
        """Check if the fighter is one of the required categories."""
        fighter: "ListFighter" = rule_inputs.fighter
        if not fighter:
            return False

        category = fighter.get_category()
        if not category:
            return False

        return category in self.fighter_categories

    def __str__(self):
        categories = ", ".join(
            FighterCategoryChoices(cat).label for cat in self.fighter_categories[:3]
        )
        if len(self.fighter_categories) > 3:
            categories += "..."
        return f"Fighter Category Rule: {categories}"

    class Meta:
        verbose_name = "Expansion Rule by Fighter Category"
        verbose_name_plural = "Expansion Rules by Fighter Category"
