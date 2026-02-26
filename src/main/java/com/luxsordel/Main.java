package com.luxsordel;

import java.util.List;

import com.luxsordel.api.HhClient;
import com.luxsordel.api.Vacancy;

/**
 * Hello world!
 *
 */
public class Main 
{
    public static void main( String[] args ) throws Exception
    {
        HhClient client = new HhClient();
        String url = "https://api.hh.ru/vacancies?text=Java&area=16&experience=noExperience";
        List<Vacancy> vacancies = client.getVacancies(url);

        System.out.println("Найдено вакансий: " + vacancies.size());
        for (Vacancy v : vacancies) {
            System.out.println(v.name() + " (" + v.employer().name() + ")");
        }
    }
}
