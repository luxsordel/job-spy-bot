package com.luxsordel.api;

import java.time.ZonedDateTime;

import com.fasterxml.jackson.annotation.JsonIgnoreProperties;
import com.fasterxml.jackson.annotation.JsonProperty;

@JsonIgnoreProperties(ignoreUnknown = true)
public record Vacancy(String id,
                    String name,
                    @JsonProperty("alternate_url") String alternate_url,
                    Salary salary,
                    Employer employer,
                    @JsonProperty("published_at") ZonedDateTime published_at) {}
