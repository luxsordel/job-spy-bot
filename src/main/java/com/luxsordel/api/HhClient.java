package com.luxsordel.api;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.util.List;

import com.fasterxml.jackson.databind.ObjectMapper;

public class HhClient {

    private final HttpClient client = HttpClient.newHttpClient();
    private final ObjectMapper mapper = new ObjectMapper()
        .registerModule(new com.fasterxml.jackson.datatype.jsr310.JavaTimeModule());

    public List<Vacancy> getVacancies(String url) throws Exception {
        String userAgent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36";

        HttpRequest request = HttpRequest.newBuilder()
            .uri(URI.create(url))
            .header("User-agent", userAgent)
            .GET()
            .build();

        HttpResponse<String> json = client.send(request, HttpResponse.BodyHandlers.ofString());
        VacancyResponse response = mapper.readValue(json.body(), VacancyResponse.class);
        
        return response.items();
    }
}