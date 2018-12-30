from django.db import models
from django.contrib.auth.models import User

# Create your models here.


class Watchers(models.Model):
	"""Watcher objects table"""
	wid = models.AutoField(primary_key=True)
	name = models.TextField()


class WatcherLogs(models.Model):
	"""Watcher logs table"""
	class Meta:
		unique_together = ("wid", "log")
	wid = models.IntegerField()
	source = models.TextField()
	log = models.TextField()


class WatcherRules(models.Model):
	"""Table which tracks all LogWatch object tables"""
	class Meta:
		unique_together = ("wid", "node_id", "rule")
	wid = models.IntegerField()
	node_id = models.IntegerField()
	rule = models.TextField()