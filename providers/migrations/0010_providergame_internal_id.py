# Generated by Django 2.2.12 on 2022-05-31 23:07

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('providers', '0009_auto_20220510_1700'),
    ]

    operations = [
        migrations.AddField(
            model_name='providergame',
            name='internal_id',
            field=models.CharField(max_length=255, null=True),
        ),
    ]
