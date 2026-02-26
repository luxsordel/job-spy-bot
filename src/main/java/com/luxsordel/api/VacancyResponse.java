package com.luxsordel.api;

import java.util.List;

import com.fasterxml.jackson.annotation.JsonIgnoreProperties;

@JsonIgnoreProperties(ignoreUnknown=true)
public record VacancyResponse(List<Vacancy> items) {}
